# standard
import os
import shutil
import time
import hashlib
from zipfile import ZipFile, is_zipfile
import asyncio

# third-party
import httpx
import aiofiles
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# local
from redfetch import config
from redfetch import special
from redfetch.models import DownloadTask
from redfetch.utils import (
    get_folder_path,
    is_safe_path,
)

# ZIP safety constants
MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024  # 2GB limit
MAX_FILES_PER_ZIP = 60000  # Safety cap to avoid zipbombs


async def download_resource_async(task: DownloadTask, client: httpx.AsyncClient) -> bool:
    folder_path = get_folder_path(
        task.resource_id,
        task.category_id,
        task.is_dependency,
        task.parent_resource_id
    )
    should_flatten = special.get_flatten_status(
        task.resource_id,
        task.is_dependency,
        task.parent_resource_id,
    )

    try:
        print(f"Starting download: {task.filename} (ID: {task.resource_id})", flush=True)
        file_path = os.path.join(folder_path, task.filename)
        ok = await download_file_async(client, task.download_url, file_path, task.file_hash)
        if not ok:
            print(f"Download failed for resource {task.resource_id}.")
            return False
        if is_zipfile(file_path):
            # Offload CPU and blocking I/O to thread
            await asyncio.to_thread(extract_and_discard_zip, file_path, folder_path, task.resource_id, should_flatten)
        return True
    except httpx.HTTPError as e:
        print(f"Failed to fetch or download resource {task.resource_id}: {str(e)}")
        return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)),
    reraise=True,
)
async def download_file_async(
    client: httpx.AsyncClient,
    download_url: str,
    file_path: str,
    expected_md5: str | None = None,
) -> bool:
    """
    Download a file to disk using an async flow:
      1. open HTTP stream
      2. stream body into a temp file with async for / await
      3. optionally verify MD5
      4. atomically move temp file into place
    """
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    hasher = hashlib.md5() if expected_md5 else None
    tmp_path = None

    try:
        async with client.stream("GET", download_url, timeout=60.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            _ensure_disk_space(resp, file_path)

            # Core async loop lives in a small helper
            tmp_path = await _stream_to_tempfile(resp, file_path, hasher)

        if expected_md5 and hasher:
            if not _hash_matches(hasher, expected_md5, file_path, tmp_path):
                return False

        if tmp_path is None:
            return False

        os.replace(tmp_path, file_path)
        print(f"Downloaded file {file_path}")
        return True
    finally:
        # Ensure no stray temp file remains on exception paths
        try:
            if tmp_path and os.path.exists(tmp_path) and not os.path.exists(file_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _ensure_disk_space(resp, file_path: str) -> None:
    """Disk space guard using Content-Length when available."""
    content_length = resp.headers.get("Content-Length")
    if content_length is None or not content_length.isdigit():
        return

    expected_size = int(content_length)
    target_dir = os.path.dirname(file_path)
    free_bytes = shutil.disk_usage(target_dir).free
    # Require a 100MB cushion
    if free_bytes < expected_size + 100 * 1024 * 1024:
        raise OSError(f"Insufficient disk space for download: need ~{expected_size} bytes")


async def _stream_to_tempfile(resp, file_path: str, hasher) -> str:
    """Stream HTTP body into a temp file."""
    tmp_dir = os.path.dirname(file_path)
    tmp_path = None

    try:
        async with aiofiles.tempfile.NamedTemporaryFile("wb", delete=False, dir=tmp_dir) as tmp:
            tmp_path = tmp.name

            async for chunk in resp.aiter_bytes(chunk_size=262_144):
                if not chunk:
                    continue

                if hasher:
                    hasher.update(chunk)

                await tmp.write(chunk)

        return tmp_path
    except Exception:
        # Clean up any partially written temp file on error
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _hash_matches(hasher, expected_md5: str, file_path: str, tmp_path: str | None) -> bool:
    """Verify MD5 hash and clean up temp file on mismatch."""
    actual = hasher.hexdigest().lower()
    # Sanitize expected_md5 in case of any whitespace issues
    expected = expected_md5.strip().lower()
    
    if actual == expected:
        return True

    if tmp_path and os.path.exists(tmp_path):
        os.remove(tmp_path)

    print(f"MD5 mismatch for {os.path.basename(file_path)}")
    print(f"  Expected: {expected}")
    print(f"  Got:      {actual}")
    return False


#
# zip functions
#

def extract_and_discard_zip(zip_path, extract_to, resource_id, should_flatten=False):
    # Check the compressed size
    zip_size = os.path.getsize(zip_path)
    if zip_size > MAX_UNCOMPRESSED_SIZE:
        print(f"ZIP file {zip_path} exceeds the 2GB size limit. Extraction aborted.")
        delete_zip_file(zip_path)
        return

    # Open the ZIP file and calculate the total uncompressed size
    with ZipFile(zip_path, 'r') as zip_ref:
        # Quick CRC integrity check before extraction
        try:
            bad_member = zip_ref.testzip()
            if bad_member is not None:
                print(f"ZIP integrity check failed at member: {bad_member}. Extraction aborted.")
                delete_zip_file(zip_path)
                return
        except Exception as e:
            print(f"ZIP integrity check failed: {e}. Extraction aborted.")
            delete_zip_file(zip_path)
            return
        infos = zip_ref.infolist()
        # Safety: cap number of files
        if len(infos) > MAX_FILES_PER_ZIP:
            print(f"ZIP has too many files ({len(infos)} > {MAX_FILES_PER_ZIP}). Extraction aborted.")
            delete_zip_file(zip_path)
            return
        total_uncompressed_size = sum([zinfo.file_size for zinfo in infos])
        if total_uncompressed_size > MAX_UNCOMPRESSED_SIZE:
            print(f"Total uncompressed size {total_uncompressed_size} exceeds the 2GB limit. Extraction aborted.")
            delete_zip_file(zip_path)
            return

        # Disk space guard before extraction (add 500MB cushion)
        try:
            free_bytes = shutil.disk_usage(extract_to).free
            if free_bytes < total_uncompressed_size + 500 * 1024 * 1024:
                print("Insufficient disk space for extraction. Extraction aborted.")
                delete_zip_file(zip_path)
                return
        except Exception:
            # If we can't determine space, proceed best-effort
            pass

        # Load protected files for the resource
        protected_files = config.settings.from_env(config.settings.ENV).PROTECTED_FILES_BY_RESOURCE.get(resource_id, [])
        if should_flatten:
            extract_flattened(zip_ref, extract_to, protected_files)
        else:
            extract_with_structure(zip_ref, extract_to, protected_files)

    delete_zip_file(zip_path)


def extract_flattened(zip_ref, extract_to, protected_files):
    print(f"Flattening extraction to {extract_to}")
    protected_files_lower = {f.lower() for f in protected_files}
    for member in zip_ref.infolist():
        filename = os.path.basename(member.filename)
        if not filename:
            continue
        target_path = os.path.join(extract_to, filename)
        normalized_path = os.path.normpath(target_path)
        if is_protected(filename, normalized_path, protected_files, protected_files_lower):
            print(f"Skipping protected file {filename}")
            continue
        if is_safe_path(extract_to, normalized_path):
            extract_zip_member(zip_ref, member, normalized_path)
        else:
            print(f"Skipping unsafe file {member.filename}")


def extract_with_structure(zip_ref, extract_to, protected_files):
    print(f"Extracting with structure to {extract_to}")
    protected_files_lower = {f.lower() for f in protected_files}
    for member in zip_ref.infolist():
        target_path = os.path.join(extract_to, member.filename)
        normalized_path = os.path.normpath(target_path)
        if not is_safe_path(extract_to, normalized_path):
            print(f"Skipping unsafe file {member.filename}")
            continue
        if is_protected(os.path.basename(member.filename), normalized_path, protected_files, protected_files_lower):
            print(f"Skipping protected file {member.filename}")
            continue
        if member.is_dir():
            os.makedirs(normalized_path, exist_ok=True)
            continue
        extract_zip_member(zip_ref, member, normalized_path)


def extract_zip_member(zip_ref, member, target_path):
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    max_retries = 3
    retry_delay = 10  # seconds

    for attempt in range(max_retries):
        try:
            with zip_ref.open(member) as source, open(target_path, 'wb') as target:
                shutil.copyfileobj(source, target)
            return  # Successful extraction, exit the function
        except PermissionError:
            file_name = os.path.basename(target_path)
            folder_path = os.path.dirname(target_path)
            
            error_msg = [
                f"\nPermission Error: Unable to extract {file_name}",
                "\nThis could be because:",
                "1. The file is currently in use by another program (e.g., MacroQuest, EQBCS)",
                "2. You don't have write permissions for this location",
                "\nPossible solutions:",
                "1. Close all EverQuest-related programs (MacroQuest, EQBCS, etc.)",
                f"2. Change the installation directory in settings to a location you own",
                f"3. Manually set write permissions on: {folder_path}",
            ]
            
            if attempt < max_retries - 1:
                error_msg.append(f"\nRetrying in {retry_delay} seconds... (Attempt {attempt + 1} of {max_retries})")
                print("\n".join(error_msg))
                time.sleep(retry_delay)
            else:
                error_msg.append("\nMaximum retry attempts reached. Please resolve the permission issue and try again.")
                print("\n".join(error_msg))
                raise PermissionError(f"Failed to extract {file_name} after {max_retries} attempts.")
        except Exception as e:
            print(f"Unexpected error while extracting {os.path.basename(target_path)}: {str(e)}")
            raise

    # If we've exhausted all retries or encountered an unexpected error,
    # stop the extraction by raising an exception
    raise Exception(f"Extraction stopped due to failure extracting {os.path.basename(target_path)}.")


def delete_zip_file(zip_path):
    try:
        os.remove(zip_path)
    except PermissionError as e:
        print(f"PermissionError: Unable to delete zip file {zip_path}. Error: {e}")


#
# utility functions
#

def is_protected(filename, target_path, protected_files, protected_files_lower):
    # Overwrite protection for specified files
    filename_lower = filename.lower()
    if filename_lower in protected_files_lower and os.path.exists(target_path):
        # Retrieve the original filename case for message consistency
        protected_list = [f for f in protected_files if f.lower() == filename_lower]
        original_filename = protected_list[0] if protected_list else filename
        print(f"Protected {original_filename}, skipping extraction.")
        return True
    return False