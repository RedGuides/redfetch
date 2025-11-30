"""
NavMesh sync module - downloads Nav mesh files from mqmesh.com
"""
import os
import hashlib
import json
import asyncio
from dataclasses import dataclass
from typing import Callable

import httpx
import aiosqlite

from redfetch import config
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


async def fetch_manifest_cached(db_path: str, client: httpx.AsyncClient) -> tuple[dict, bool]:
    """Fetch the navmesh manifest with HTTP caching."""
    current_env = config.settings.ENV

    # Load cached headers from DB
    cached_etag = None
    cached_last_modified = None
    cached_manifest_json = None

    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        async with conn.execute(
            "SELECT etag, last_modified, manifest_json FROM navmesh_cache WHERE env = ?",
            (current_env,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                cached_etag, cached_last_modified, cached_manifest_json = row

    # Build request headers for conditional GET
    headers = {}
    if cached_etag:
        headers["If-None-Match"] = cached_etag
    if cached_last_modified:
        headers["If-Modified-Since"] = cached_last_modified

    response = await client.get(NAVMESH_MANIFEST_URL, headers=headers)

    if response.status_code == 304:
        # Not Modified - use cached manifest
        if cached_manifest_json:
            return json.loads(cached_manifest_json), False
        # Fallback: if we got 304 but have no cached manifest, fetch fresh
        response = await client.get(NAVMESH_MANIFEST_URL)

    response.raise_for_status()

    # Parse new manifest
    manifest = response.json()

    # Save new caching headers and manifest to DB
    new_etag = response.headers.get("ETag")
    new_last_modified = response.headers.get("Last-Modified")

    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await conn.execute(
            """
            INSERT INTO navmesh_cache (env, etag, last_modified, manifest_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(env) DO UPDATE SET
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                manifest_json = excluded.manifest_json
            """,
            (current_env, new_etag, new_last_modified, json.dumps(manifest))
        )
        await conn.commit()

    return manifest, True


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
        hasher = hashlib.md5()
        with open(file_path, "rb") as f:
            while chunk := f.read(262_144):
                hasher.update(chunk)
        return hasher.hexdigest().lower()
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
    on_event: Callable | None = None,
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
            # Tier 1: Fetch manifest (may be cached); always validate local state
            manifest, _ = await fetch_manifest_cached(db_path, client)

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

            # Download in parallel (batch size 5 to respect server limits)
            BATCH_SIZE = 5
            failed = 0
            
            for i in range(0, len(to_download), BATCH_SIZE):
                batch = to_download[i : i + BATCH_SIZE]
                tasks = []
                for nm in batch:
                    print(f"navmesh: Downloading {nm.filename}...")
                    tasks.append(download_navmesh_file(client, nm, navmesh_dir, db_path))
                
                results = await asyncio.gather(*tasks)
                
                # Process results
                for nm, ok in zip(batch, results):
                    if ok:
                        if on_event:
                            on_event(("done", nm.filename, "downloaded"))
                    else:
                        if on_event:
                            on_event(("done", nm.filename, "error"))
                        failed += 1

            if failed:
                print(f"navmesh: {failed} meshes failed to download")
                # Invalidate manifest cache so next run re-checks everything
                async with aiosqlite.connect(db_path, timeout=30.0) as conn:
                    await conn.execute(
                        "DELETE FROM navmesh_cache WHERE env = ?",
                        (config.settings.ENV,)
                    )
                    await conn.commit()
                return False

            print("All navmesh files downloaded successfully")
            return True

        except (httpx.HTTPError, json.JSONDecodeError) as e:
            print(f"navmesh sync warning: Error during navmesh sync: {e}")
            try:
                async with aiosqlite.connect(db_path, timeout=30.0) as conn:
                    await conn.execute(
                        "DELETE FROM navmesh_cache WHERE env = ?",
                        (config.settings.ENV,),
                    )
                    await conn.commit()
            except Exception as cache_err:
                print(f"Warning: Failed to clear navmesh manifest cache after error: {cache_err}")
            return False

