from typing import Dict, Iterable, List, Optional, Set, Tuple, Callable
import asyncio

import httpx
import aiosqlite

from redfetch import api
from redfetch import config
from redfetch import download
from redfetch import net
from redfetch import store
from redfetch.models import DownloadTask, FileInfo, Resource
from redfetch.special import SpecialResourceInfo, compute_special_status


SpecialStatus = Dict[str, SpecialResourceInfo]

# Centralized limits for download concurrency; adjust here to control
# how many concurrent connections hit the server during the download phase.
DOWNLOAD_MAX_CONNECTIONS = 6
DOWNLOAD_MAX_KEEPALIVE_CONNECTIONS = 6


def resource_from_api_payload(payload: dict, *, is_watching: bool = False, is_special: bool = False, is_licensed: bool = False) -> Resource:
    """Convert API payload to Resource."""
    file = payload['current_files'][0]
    raw_hash = file.get('hash')
    # Sanitize hash: strip whitespace and validate it's hexadecimal
    sanitized_hash = None
    if raw_hash:
        cleaned = raw_hash.strip().lower()
        # Validate it's a valid MD5 (32 hex chars)
        if len(cleaned) == 32 and all(c in '0123456789abcdef' for c in cleaned):
            sanitized_hash = cleaned
    
    file_info = FileInfo(
        filename=file['filename'],
        url=file['download_url'],
        hash=sanitized_hash
    )
    category_id = payload['Category']['parent_category_id']
    return Resource(
        resource_id=str(payload['resource_id']),
        parent_id=None,
        title=payload['title'],
        category_id=category_id,
        version=file['id'],
        file=file_info,
        is_watching=is_watching,
        is_special=is_special,
        is_licensed=is_licensed,
    )


async def _process_resources(conn: "aiosqlite.Connection", resources: Iterable[dict]) -> Set[Tuple[Optional[int], int]]:
    current_ids: Set[Tuple[Optional[int], int]] = set()
    current_env = config.settings.ENV
    for resource_payload in resources:
        if not resource_payload:
            continue
        resource = resource_from_api_payload(resource_payload, is_watching=True)
        resource_id = resource.resource_id
        parent_category_id = resource.category_id
        if parent_category_id == 11 and current_env in ['TEST', 'EMU']:
            continue
        if parent_category_id in config.CATEGORY_MAP:
            await store.insert_prepared_resource(conn, resource, is_special=False, is_dependency=False, parent_id=None, license_details=None)
            current_ids.add((None, int(resource_id)))
    return current_ids


async def _process_licensed_resources(conn: "aiosqlite.Connection", licensed_resources: Iterable[dict]) -> Set[Tuple[Optional[int], int]]:
    current_ids: Set[Tuple[Optional[int], int]] = set()
    current_env = config.settings.ENV
    for license_info in licensed_resources:
        resource_payload = license_info['resource']
        resource = resource_from_api_payload(resource_payload, is_licensed=bool(license_info['active']))
        parent_category_id = resource.category_id
        if parent_category_id == 11 and current_env in ['TEST', 'EMU']:
            continue
        license_details = {
            'active': license_info['active'],
            'start_date': license_info.get('start_date'),
            'end_date': license_info.get('end_date'),
            'license_id': license_info['license_id']
        }
        if parent_category_id in config.CATEGORY_MAP:
            await store.insert_prepared_resource(conn, resource, is_special=False, is_dependency=False, parent_id=None, license_details=license_details)
            current_ids.add((None, int(resource.resource_id)))
    return current_ids


async def _process_special_resources(conn: "aiosqlite.Connection", special_resource_status: SpecialStatus, special_resources_data: Iterable[dict]) -> Set[Tuple[Optional[int], int]]:
    current_ids: Set[Tuple[Optional[int], int]] = set()

    for res_id, status in special_resource_status.items():
        is_special = bool(status['is_special'])
        parent_ids = status['parent_ids']
        # Track standalone entry if resource is special (regardless of whether it's also a dependency)
        if is_special:
            current_ids.add((None, int(res_id)))
        # Track each dependency relationship
        for parent_id in parent_ids:
            current_ids.add((int(parent_id), int(res_id)))

    for resource_payload in special_resources_data:
        res_id = str(resource_payload['resource_id'])
        if res_id not in special_resource_status:
            continue
        status = special_resource_status[res_id]
        is_special = bool(status['is_special'])
        parent_ids = status['parent_ids']
        resource = resource_from_api_payload(resource_payload, is_special=is_special)
        
        # Insert as standalone if it's special (regardless of whether it also has parents)
        if is_special:
            await store.insert_prepared_resource(conn, resource, is_special, is_dependency=False, parent_id=None, license_details=None)
        
        # ALSO insert as dependency for each parent
        for parent_id in parent_ids:
            await store.insert_prepared_resource(conn, resource, is_special, is_dependency=True, parent_id=parent_id, license_details=None)
    return current_ids


async def _filter_special_resources_async(conn: "aiosqlite.Connection", special_resource_status: SpecialStatus, manifest: dict) -> List[str]:
    manifest_resources = manifest.get('resources', {})
    to_fetch: List[str] = []

    last_fetch_time = await store.get_last_fetch_time(conn)

    for res_id in special_resource_status.keys():
        entry = manifest_resources.get(str(res_id))
        if entry and entry.get('last_update', 0) > last_fetch_time:
            to_fetch.append(res_id)
            continue
        try:
            if not await store.has_root_download_row_for_resource(conn, int(res_id)):
                to_fetch.append(res_id)
        except Exception:
            to_fetch.append(res_id)
    return to_fetch


async def _filter_ids_needing_fetch(conn: "aiosqlite.Connection", all_ids: List[str], special_resource_status: SpecialStatus, manifest: dict) -> List[str]:
    """Filter to IDs that changed or are missing (via manifest/DB checks)."""
    manifest_resources = manifest.get('resources', {})
    last_fetch_time = await store.get_last_fetch_time(conn)
    fetch_ids = []
    
    for rid in all_ids:
        status = special_resource_status.get(rid, {})
        is_dependency = bool(status.get('is_dependency'))
        parent_ids: Set[int] = set(int(pid) for pid in status.get('parent_ids', set()))

        # Presence check in DB
        if is_dependency:
            has_db_entry = await store.has_dependency_rows(conn, int(rid), parent_ids)
        else:
            has_db_entry = await store.has_root_download_row_for_resource(conn, int(rid))

        if not has_db_entry:
            fetch_ids.append(rid)
            continue

        # Manifest freshness check
        entry = manifest_resources.get(str(rid))
        if entry and int(entry.get('last_update', 0)) <= last_fetch_time:
            # Unchanged since last fetch; skip API call
            continue

        fetch_ids.append(rid)
    
    return fetch_ids


async def _fetch_from_api_async(
    conn: "aiosqlite.Connection",
    headers: dict,
    resource_ids: Optional[List[str]] = None,
) -> dict:
    """Fetch API data needed for a full or partial sync."""
    async with httpx.AsyncClient(
        headers=headers,
        http2=True,
        timeout=30.0,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        if resource_ids is None:
            return await _fetch_full_sync_data(conn, client)
        return await _fetch_partial_sync_data(conn, client, resource_ids)


async def _fetch_full_sync_data(conn: "aiosqlite.Connection", client: httpx.AsyncClient) -> dict:
    """Fetch watched resources, licenses, and manifest for a full sync."""
    watched_resources, licensed_resources, manifest = await asyncio.gather(
        api.fetch_watched_resources(client),
        api.fetch_licenses(client),
        net.fetch_manifest_cached(client),
    )

    # CPU-bound work (no I/O, can run sync)
    special_resource_status = compute_special_status(None)

    # Filter and fetch special resources (using manifest to skip unchanged)
    special_ids = await _filter_special_resources_async(conn, special_resource_status, manifest)
    special_resources_data = await api.fetch_resources_batch(client, special_ids)

    return {
        "watched_resources": watched_resources,
        "licensed_resources": licensed_resources,
        "special_resource_status": special_resource_status,
        "special_resources_data": special_resources_data,
    }


async def _fetch_partial_sync_data(
    conn: "aiosqlite.Connection",
    client: httpx.AsyncClient,
    resource_ids: List[str],
) -> dict:
    """Fetch only the requested resources that need updating."""
    # Use manifest to skip unchanged resources
    manifest = await net.fetch_manifest_cached(client)
    licensed_resources: list = []

    # Get special/dependency status for all requested resources
    special_resource_status = compute_special_status(resource_ids)
    all_ids = list(special_resource_status.keys())

    # Filter to only fetch resources that have changed (uses manifest)
    fetch_ids = await _filter_ids_needing_fetch(conn, all_ids, special_resource_status, manifest)

    # Fetch only what we need
    all_fetched = await api.fetch_resources_batch(client, fetch_ids) if fetch_ids else []

    # Separate into special vs regular resources for processing
    special_ids_set = {
        rid
        for rid, status in special_resource_status.items()
        if status.get("is_special") or status.get("is_dependency")
    }
    special_resources_data = [
        res for res in all_fetched if str(res.get("resource_id")) in special_ids_set
    ]
    watched_resources = [
        res for res in all_fetched if str(res.get("resource_id")) not in special_ids_set
    ]

    return {
        "watched_resources": watched_resources,
        "licensed_resources": licensed_resources,
        "special_resource_status": special_resource_status,
        "special_resources_data": special_resources_data,
    }


async def _store_fetched_data_async(conn: "aiosqlite.Connection", fetched_data: dict) -> Set[Tuple[Optional[int], int]]:
    current_ids = await _process_resources(conn, fetched_data['watched_resources'])
    current_ids.update(await _process_licensed_resources(conn, fetched_data['licensed_resources']))
    current_ids.update(await _process_special_resources(conn, fetched_data['special_resource_status'], fetched_data['special_resources_data']))
    return current_ids

async def _download_and_update(
    db_path: str,
    headers: dict,
    to_download: List[DownloadTask],
    on_event: Optional[Callable],
) -> List[Tuple[str, str]]:
    """Download queued resources concurrently and update the DB."""
    results: List[Tuple[str, str]] = []
    pending_updates: List[Tuple[str, int, bool, Optional[str]]] = []

    async with httpx.AsyncClient(
        headers=headers,
        http2=True,
        timeout=60.0,
        limits=httpx.Limits(
            max_connections=DOWNLOAD_MAX_CONNECTIONS,
            max_keepalive_connections=DOWNLOAD_MAX_KEEPALIVE_CONNECTIONS,
        ),
    ) as client:
        # One coroutine per download task; concurrency is bounded by httpx limits.
        download_coroutines = [
            _download_one(task, client, on_event, results, pending_updates)
            for task in to_download
        ]

        # Textbook-style: rely on task cancellation to bubble up naturally
        await asyncio.gather(*download_coroutines)

    # Apply DB updates after all downloads complete
    if db_path and pending_updates:
        try:
            await store.apply_updates(db_path, pending_updates)
        except Exception as e:
            print(f"Error applying DB updates: {e}")
    return results


async def _download_one(
    task: DownloadTask,
    client: httpx.AsyncClient,
    on_event: Optional[Callable],
    results: List[Tuple[str, str]],
    pending_updates: List[Tuple[str, int, bool, Optional[str]]],
) -> None:
    """Download a single resource."""
    try:
        ok = await download.download_resource_async(task, client)
    except Exception as e:
        ok = False
        print(f"Error downloading {task.resource_id}: {e}")

    if ok:
        if on_event:
            on_event(("done", task.resource_id, "downloaded"))
        results.append((task.resource_id, "downloaded"))
        pending_updates.append(
            (task.resource_id, int(task.remote_version), bool(task.is_dependency), task.parent_resource_id)
        )
    else:
        if on_event:
            on_event(("done", task.resource_id, "error"))
        results.append((task.resource_id, "error"))


async def sync(
    db_path: str,
    headers: dict,
    resource_ids: Optional[List[str]] = None,
    on_event: Optional[Callable] = None,
) -> bool:
    """Synchronize local DB state with the API and download any needed files."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        fetched_data = await _fetch_from_api_async(conn, headers, resource_ids)

        tasks: List[DownloadTask] = []
        await conn.execute("BEGIN")
        try:
            current_ids = await _store_fetched_data_async(conn, fetched_data)

            # Fetch DownloadTask objects from DB
            if resource_ids is not None:
                for rid in resource_ids:
                    tasks.extend(await store.fetch_single_db_resource(conn, rid))
            else:
                await store.clean_up_unnecessary_resources(conn, current_ids)
                tasks = await store.fetch_watched_db_resources(conn)

            # For single-resource runs, update last_fetch_time so manifest fast-paths work next time
            if resource_ids is not None:
                try:
                    await conn.execute(
                        "UPDATE metadata SET last_fetch_time = strftime('%s','now') WHERE id = 1"
                    )
                except Exception:
                    # If the metadata table is missing or the update fails,
                    # we still want the rest of the sync to proceed.
                    pass
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    # If specific IDs were requested and no tasks were found, they are invalid/non-downloadable
    if resource_ids is not None and not tasks:
        print(f"No valid resources found for IDs: {resource_ids}. Are you in the right server env? Did you opt_in in your settings.local.toml?")
        return False

    print(f"Total resources to process: >>> {len(tasks)} <<<")

    download_results: List[Tuple[str, str]] = []
    try:
        to_download: List[DownloadTask] = []
        for task in tasks:
            if task.needs_download:
                title = getattr(task, 'title', None)
                resource_display = f"{title} (ID: {task.resource_id})" if title else f"resource {task.resource_id}"
                print(f"Queued for download: {resource_display}")
                if on_event:
                    on_event(("start", task.resource_id, title))
                to_download.append(task)
            else:
                if on_event:
                    on_event(("done", task.resource_id, "skipped"))
                download_results.append((task.resource_id, 'skipped'))

        if to_download:
            dl_results = await _download_and_update(db_path, headers, to_download, on_event)
            download_results.extend(dl_results)
    except KeyboardInterrupt:
        print("\nDownload process was cancelled by user.")
        return False
    except asyncio.CancelledError:
        print("\nDownload process was cancelled by user.")
        return False

    errored_resources = [res_id for res_id, res in download_results if res == 'error']
    downloaded_resources = [res_id for res_id, res in download_results if res == 'downloaded']

    if errored_resources:
        print("One or more resources failed to download.")
        print(f"Failed resources: {errored_resources}")
        return False
    elif downloaded_resources:
        print("All resources downloaded successfully.")
        return True
    else:
        print("All resources are up-to-date; no downloads were necessary.")
        return True


async def run_sync(db_path: str, headers: dict, resource_ids: Optional[List[str]] = None, on_event: Optional[Callable] = None) -> bool:
    """Run the async sync pipeline."""
    try:
        return await sync(db_path, headers, resource_ids=resource_ids, on_event=on_event)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Download cancelled by user.")
        return False

