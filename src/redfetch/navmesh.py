"""
NavMesh sync module - downloads Nav mesh files from mqmesh.com
"""
import os
import hashlib
import json
import asyncio
from dataclasses import dataclass
import httpx
import aiosqlite
from hishel import AsyncSqliteStorage
from hishel.httpx import AsyncCacheClient

from redfetch import config
from redfetch.sync_types import SyncEventCallback
from redfetch.utils import get_vvmq_path
from redfetch.download import download_file_async


NAVMESH_MANIFEST_URL = "https://mqmesh.com/updater.json"


@dataclass
class NavMeshFile:
    """Represents a navmesh file to potentially download."""
    zone_name: str           # e.g., "gfaydark"
    filename: str            # e.g., "gfaydark.navmesh"
    download_url: str        # Full URL
    remote_hash: str         # MD5 from manifest
    local_hash: str | None = None  # MD5 of local file (if exists)

    @property
    def needs_download(self) -> bool:
        return self.local_hash is None or self.local_hash != self.remote_hash


def get_navmesh_directory() -> str | None:
    """Get the navmesh installation directory based on current environment."""
    vvmq_path = get_vvmq_path()
    if not vvmq_path:
        return None
    return os.path.join(vvmq_path, "resources", "MQ2Nav")


def get_navmesh_opt_in() -> bool | None:
    """Get the navmesh opt-in setting for the current environment."""
    try:
        return config.settings.from_env(config.settings.ENV).get("NAVMESH_OPT_IN", None)
    except Exception:
        return None


def is_navmesh_enabled(override: bool | None = None) -> bool:
    """Check if navmesh sync is enabled for the current environment."""
    if override is not None:
        return override
    opt_in = get_navmesh_opt_in()
    return opt_in is True  # Only True if explicitly set to True


def get_protected_navmeshes() -> list[str]:
    """Get list of protected navmesh filenames (case-insensitive) for current env."""
    try:
        protected = config.settings.from_env(config.settings.ENV).PROTECTED_FILES_BY_RESOURCE.get("navmesh", [])
        return [f.lower() for f in protected]
    except Exception:
        return []


def _manifest_cache_path(db_path: str) -> str:
    """Sibling SQLite file holding hishel's manifest HTTP cache."""
    return os.path.join(os.path.dirname(db_path), "navmesh_http_cache.sqlite")


async def fetch_manifest(db_path: str) -> dict:
    """Fetch and return the latest navmesh manifest from the server, bypassing any stale cache."""
    storage = AsyncSqliteStorage(database_path=_manifest_cache_path(db_path))
    async with AsyncCacheClient(timeout=30.0, storage=storage) as client:
        response = await client.get(
            NAVMESH_MANIFEST_URL, headers={"Cache-Control": "no-cache"}
        )
        response.raise_for_status()
        return response.json()


async def get_local_navmesh_state(db_path: str, navmesh_dir: str) -> dict[str, str]:
    """ Get hash map of local navmesh file """
    result: dict[str, str] = {}

    if not os.path.exists(navmesh_dir):
        return result

    # Scan local files
    local_files: list[tuple[str, int, int]] = []  # (filename, size, mtime_ns)
    try:
        for entry in os.scandir(navmesh_dir):
            if entry.is_file() and entry.name.endswith(".navmesh"):
                stat = entry.stat()
                local_files.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return result

    if not local_files:
        return result

    # Load cached hashes from DB
    cached_records: dict[str, tuple[str, int, int]] = {}  # filename -> (hash, size, mtime_ns)
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        async with conn.execute(
            "SELECT filename, md5_hash, file_size, mtime_ns FROM navmesh_files"
        ) as cur:
            async for row in cur:
                filename, md5_hash, file_size, mtime_ns = row
                cached_records[filename] = (md5_hash, file_size, mtime_ns)

    # Process each local file
    to_update: list[tuple[str, str, int, int]] = []  # (filename, hash, size, mtime_ns)

    for filename, size, mtime_ns in local_files:
        cached = cached_records.get(filename)
        if cached and cached[1] == size and cached[2] == mtime_ns:
            # File unchanged, use cached hash
            result[filename] = cached[0]
        else:
            # File changed or new, re-hash it
            file_path = os.path.join(navmesh_dir, filename)
            file_hash = _hash_file(file_path)
            if file_hash:
                result[filename] = file_hash
                to_update.append((filename, file_hash, size, mtime_ns))

    # Update DB with new/changed hashes
    if to_update:
        async with aiosqlite.connect(db_path, timeout=30.0) as conn:
            await conn.executemany(
                """
                INSERT INTO navmesh_files (filename, md5_hash, file_size, mtime_ns)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    md5_hash = excluded.md5_hash,
                    file_size = excluded.file_size,
                    mtime_ns = excluded.mtime_ns
                """,
                to_update
            )
            await conn.commit()

    return result


def _hash_file(file_path: str) -> str | None:
    """Compute MD5 hash of a file."""
    try:
        with open(file_path, "rb") as f:
            digest = hashlib.file_digest(f, "md5")
        return digest.hexdigest().lower()
    except OSError:
        return None


async def download_navmesh_file(
    client: httpx.AsyncClient,
    nm: NavMeshFile,
    navmesh_dir: str,
    db_path: str
) -> bool:
    """Download a single navmesh file."""
    file_path = os.path.join(navmesh_dir, nm.filename)

    ok = await download_file_async(
        client,
        nm.download_url,
        file_path,
        expected_md5=nm.remote_hash
    )

    if ok:
        # Update local cache in DB
        try:
            stat = os.stat(file_path)
            file_size = stat.st_size
            mtime_ns = stat.st_mtime_ns
            async with aiosqlite.connect(db_path, timeout=30.0) as conn:
                await conn.execute(
                    """
                    INSERT INTO navmesh_files (filename, md5_hash, file_size, mtime_ns)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(filename) DO UPDATE SET
                        md5_hash = excluded.md5_hash,
                        file_size = excluded.file_size,
                        mtime_ns = excluded.mtime_ns
                    """,
                    (nm.filename, nm.remote_hash, file_size, mtime_ns)
                )
                await conn.commit()
        except Exception as e:
            print(f"Warning: Failed to update navmesh cache for {nm.filename}: {e}")

    return ok


async def sync_navmeshes(
    db_path: str,
    headers: dict,
    on_event: SyncEventCallback | None = None,
    override: bool | None = None,
) -> bool:
    """ Main entry point for navmesh sync. """
    if not is_navmesh_enabled(override):
        return True  # Nothing to do, not an error

    print("Checking navmesh files...")

    navmesh_dir = get_navmesh_directory()
    if not navmesh_dir:
        print("navmesh sync skipped: VanillaMQ path not configured")
        return True

    try:
        os.makedirs(navmesh_dir, exist_ok=True)
    except OSError as e:
        print(f"navmesh sync error: Could not create directory {navmesh_dir}: {e}")
        return True  # Don't fail the whole sync for navmesh issues

    protected_meshes = get_protected_navmeshes()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Tier 1: Fetch manifest (revalidated each run); always validate local state
            manifest = await fetch_manifest(db_path)

            # Tier 2: Get local file state (using cached hashes where possible)
            local_state = await get_local_navmesh_state(db_path, navmesh_dir)

            # Build download list
            to_download: list[NavMeshFile] = []
            zones = manifest.get("zones", {})

            for zone_name, zone_data in zones.items():
                mesh_info = zone_data.get("files", {}).get("mesh", {})
                if not mesh_info:
                    continue

                filename = f"{zone_name}.navmesh"
                download_url = mesh_info.get("link", "")
                remote_hash = mesh_info.get("hash", "").lower()

                if not download_url or not remote_hash:
                    continue

                # Check if file is protected (only skip if file already exists)
                file_path = os.path.join(navmesh_dir, filename)
                if filename.lower() in protected_meshes and os.path.exists(file_path):
                    print(f"navmesh: Skipping protected mesh {filename}")
                    continue

                nm = NavMeshFile(
                    zone_name=zone_name,
                    filename=filename,
                    download_url=download_url,
                    remote_hash=remote_hash,
                    local_hash=local_state.get(filename),
                )

                if nm.needs_download:
                    to_download.append(nm)

            if not to_download:
                print(f"All {len(zones)} navmesh files up-to-date.")
                return True

            print(f"navmesh: {len(to_download)} files to download out of {len(zones)} total")

            # Notify progress bar of additional items
            if on_event:
                on_event(("add_total", len(to_download), None))

            # Download up to 5 files at once; show progress after each finishes.
     
            semaphore = asyncio.Semaphore(5)

            async def _download_one(nm: NavMeshFile) -> bool:
                async with semaphore:
                    print(f"navmesh: Downloading {nm.filename}...")
                    ok = await download_navmesh_file(client, nm, navmesh_dir, db_path)
                if on_event:
                    on_event(("done", nm.filename, "downloaded" if ok else "error"))
                return ok

            results = await asyncio.gather(*(_download_one(nm) for nm in to_download))
            failed = sum(1 for ok in results if not ok)

            if failed:
                # No cache invalidation needed; files are rechecked next run.
         
                print(f"navmesh: {failed} meshes failed to download")
                return False

            print("All navmesh files downloaded successfully")
            return True

        except (httpx.HTTPError, json.JSONDecodeError) as e:
            print(f"navmesh sync warning: Error during navmesh sync: {e}")
            return False

