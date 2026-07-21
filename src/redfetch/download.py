# standard
import os
import shutil
import hashlib
import stat
import sys
import time
import zlib
from pathlib import Path
from zipfile import BadZipFile, ZipFile, is_zipfile
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
from redfetch.utils import is_safe_path

# ZIP safety constants
MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024  # 2GB limit
MAX_FILES_PER_ZIP = 60000  # Safety cap to avoid zipbombs


async def download_install_target_async(
    *,
    client: httpx.AsyncClient,
    resource_id: str,
    download_url: str,
    filename: str,
    file_hash: str | None,
    folder_path: str,
    should_flatten: bool = False,
    protected_files: list[str] | None = None,
) -> bool:
    try:
        print(f"Starting download: {filename} (ID: {resource_id})", flush=True)
        file_path = os.path.join(folder_path, filename)
        ok = await download_file_async(client, download_url, file_path, file_hash)
        if not ok:
            print(f"Download failed for resource {resource_id}.")
            return False
        if is_zipfile(file_path):
            extracted = await asyncio.to_thread(
                extract_and_discard_zip,
                file_path,
                folder_path,
                resource_id,
                should_flatten,
                protected_files,
            )
            if not extracted:
                print(f"Extraction failed for resource {resource_id}.")
                return False
        return True
    except httpx.HTTPError as e:
        print(f"Failed to fetch or download resource {resource_id}: {str(e)}")
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
            if not _hash_matches(hasher, expected_md5, file_path):
                return False

        if tmp_path is None:
            return False

        try:
            displaced = _swap_into_place(tmp_path, file_path)
        except PermissionError:
            _report_locked_target(file_path)
            return False
        except OSError as e:
            print(f"Could not update {os.path.basename(file_path)}: {e}")
            _remove_if_exists(tmp_path)
            return False
        if displaced:
            print(f"Staged update for {os.path.basename(file_path)}; applies on next launch.")
        print(f"Downloaded file {file_path}")
        return True
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
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


def _hash_matches(hasher, expected_md5: str, file_path: str) -> bool:
    """Return True when the computed MD5 matches *expected_md5*."""
    actual = hasher.hexdigest().lower()
    expected = expected_md5.strip().lower()

    if actual == expected:
        return True

    print(f"MD5 mismatch for {os.path.basename(file_path)}")
    print(f"  Expected: {expected}")
    print(f"  Got:      {actual}")
    return False


#
# zip functions
#

def extract_and_discard_zip(zip_path, extract_to, resource_id, should_flatten=False, protected_files=None) -> bool:
    try:
        zip_size = os.path.getsize(zip_path)
        if zip_size > MAX_UNCOMPRESSED_SIZE:
            print(f"ZIP file {zip_path} exceeds the 2GB size limit. Extraction aborted.")
            return False

        with ZipFile(zip_path, 'r') as zip_ref:
            infos = zip_ref.infolist()
            if len(infos) > MAX_FILES_PER_ZIP:
                print(f"ZIP has too many files ({len(infos)} > {MAX_FILES_PER_ZIP}). Extraction aborted.")
                return False

            total_uncompressed_size = sum(zinfo.file_size for zinfo in infos)
            if total_uncompressed_size > MAX_UNCOMPRESSED_SIZE:
                print(f"Total uncompressed size {total_uncompressed_size} exceeds the 2GB limit. Extraction aborted.")
                return False

            try:
                free_bytes = shutil.disk_usage(extract_to).free
                if free_bytes < total_uncompressed_size + 500 * 1024 * 1024:
                    print("Insufficient disk space for extraction. Extraction aborted.")
                    return False
            except Exception:
                pass

            if protected_files is None:
                protected_files = config.settings.from_env(config.settings.ENV).PROTECTED_FILES_BY_RESOURCE.get(resource_id, [])
            sweep_stale_swap_files(extract_to)
            try:
                if should_flatten:
                    extract_flattened(zip_ref, extract_to, protected_files)
                else:
                    extract_with_structure(zip_ref, extract_to, protected_files)
            except (BadZipFile, zlib.error) as e:  # bad CRC raises BadZipFile; truncated deflate raises zlib.error
                print(f"ZIP integrity check failed: {e}. Extraction aborted.")
                return False
            return True
    finally:
        delete_zip_file(zip_path)


def extract_flattened(zip_ref, extract_to, protected_files):
    print(f"Flattening extraction to {extract_to}")
    protected_files_lower = {f.lower() for f in protected_files}
    pairs = []
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
            pairs.append((member, normalized_path))
        else:
            print(f"Skipping unsafe file {member.filename}")
    _extract_members_staged(zip_ref, pairs)


def extract_with_structure(zip_ref, extract_to, protected_files):
    print(f"Extracting with structure to {extract_to}")
    protected_files_lower = {f.lower() for f in protected_files}
    pairs = []
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
        pairs.append((member, normalized_path))
    _extract_members_staged(zip_ref, pairs)


def _extract_members_staged(zip_ref, pairs) -> None:
    """Stage every member to a sibling ``.rfnew``, then swap all in."""
    # duplicate targets, last member wins
    fold = str.lower if sys.platform == "darwin" else os.path.normcase
    deduped = {fold(target): (member, target) for member, target in pairs}
    staged = []  # (tmp_path, target_path)
    try:
        for member, target_path in deduped.values():
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            if _member_matches_disk(zip_ref, member, target_path):
                continue
            # sibling temp: same volume, so the swap is an atomic rename
            tmp_path = target_path + ".rfnew"
            try:
                with zip_ref.open(member) as source, open(tmp_path, 'wb') as target:
                    shutil.copyfileobj(source, target)
            except BaseException:
                _remove_if_exists(tmp_path)
                raise
            staged.append((tmp_path, target_path))
    except BaseException as e:  # cancellation must clean up too
        for tmp_path, _ in staged:
            _remove_if_exists(tmp_path)
        if isinstance(e, Exception):
            print(f"Extraction failed while staging; no files were changed: {e}")
        raise

    failed = []
    for tmp_path, target_path in staged:
        try:
            displaced = _swap_into_place(tmp_path, target_path)
        except PermissionError:
            _report_locked_target(target_path)
            failed.append(target_path)
            continue
        except OSError as e:  # e.g. AV quarantined the .rfnew; don't abort the rest
            print(f"Could not update {os.path.basename(target_path)}: {e}")
            _remove_if_exists(tmp_path)
            failed.append(target_path)
            continue
        if displaced:
            print(f"Staged update for {os.path.basename(target_path)}; applies on next launch.")
    if failed:
        names = ", ".join(os.path.basename(path) for path in failed)
        raise OSError(f"Could not update file(s): {names}")


def _swap_into_place(tmp_path: str, target_path: str) -> bool:
    """On Windows, files that are in use (like loaded executables) can't be overwritten directly. 
    Instead, we rename the locked file out of the way and put the new one in its place."""
    try:
        os.replace(tmp_path, target_path)
        return False
    except PermissionError:
        pass

    if os.path.isdir(target_path):
        # a directory shadowing a file member
        _remove_if_exists(tmp_path)
        raise IsADirectoryError(f"Target is a directory: {target_path}")

    # a read-only target fails the same way a locked one does
    cleared_readonly = _clear_readonly(target_path)
    if cleared_readonly:
        try:
            os.replace(tmp_path, target_path)
            return False
        except PermissionError:
            pass

    old_path = _displacement_path(target_path)
    try:
        os.replace(target_path, old_path)
    except PermissionError:
        if cleared_readonly:
            _restore_readonly(target_path)
        _remove_if_exists(tmp_path)
        raise
    _mark_fresh(old_path)  # rename kept the old mtime; a concurrent sweep would eat it
    try:
        os.replace(tmp_path, target_path)
    except BaseException:
        # roll back so the target never goes missing
        try:
            os.replace(old_path, target_path)
        except OSError:
            pass
        else:
            if cleared_readonly:
                _restore_readonly(target_path)
        _remove_if_exists(tmp_path)
        raise
    return True


def _displacement_path(target_path: str) -> str:
    """Pick a free name for the displaced old file."""
    for n in range(1000):
        candidate = f"{target_path}.rfold{n or ''}"
        _remove_if_exists(candidate)
        if not os.path.exists(candidate):
            return candidate
    return candidate  # 1000 stuck generations: let the swap fail loudly


def _member_matches_disk(zip_ref, member, target_path: str) -> bool:
    """checks if the file on disk matches the zip member, to skip the swap"""
    try:
        if not os.path.isfile(target_path) or os.path.getsize(target_path) != member.file_size:
            return False
        crc = 0
        with open(target_path, 'rb') as existing:
            for chunk in iter(lambda: existing.read(262_144), b""):
                crc = zlib.crc32(chunk, crc)
        return crc == member.CRC
    except OSError:
        return False


def _clear_readonly(path: str) -> bool:
    """True if *path* was read-only and the attribute was cleared."""
    try:
        if os.access(path, os.W_OK):
            return False
        os.chmod(path, stat.S_IWRITE)
        return True
    except OSError:
        return False


def _restore_readonly(path: str) -> None:
    """Put back a read-only bit we cleared for a swap that then failed."""
    try:
        os.chmod(path, stat.S_IREAD)
    except OSError:
        pass


def _mark_fresh(path: str) -> None:
    """Stamp now so the mtime sweep guard sees an in-flight file, not stale debris."""
    try:
        os.utime(path)
    except OSError:
        pass


def _remove_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except PermissionError:
        # the read-only bit survives displacement renames and blocks deletion
        try:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
        except OSError:
            pass
    except OSError:
        pass


_STALE_DEBRIS_AGE = 3600  # seconds


def sweep_stale_swap_files(directory: str) -> None:
    """Recursively remove .rfnew/.rfold debris left by prior runs."""
    cutoff = time.time() - _STALE_DEBRIS_AGE
    root = Path(directory)
    # rglob does not interpret glob wildcards within directory names, e.g. "Very[Vanilla]MQ" is matched literally
    for pattern in ("*.rfnew", "*.rfold*"):
        for path in root.rglob(pattern):
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
            except OSError:
                continue
            _remove_if_exists(str(path))


def _report_locked_target(target_path: str) -> None:
    """Friendly report for swap failure."""
    file_name = os.path.basename(target_path)
    folder_path = os.path.dirname(target_path)
    print("\n".join([
        f"\nPermission Error: Unable to update {file_name}",
        "\nThe file is held open by another program and can't be replaced.",
        "\nPossible solutions:",
        "1. Close the program holding it open (e.g. one keeping a database file open)",
        "2. Change the installation directory in settings to a location you own",
        f"3. Manually set write permissions on: {folder_path}",
    ]))


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