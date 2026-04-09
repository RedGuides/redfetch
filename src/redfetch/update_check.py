"""Lightweight non-interactive update check, no downloads"""

from __future__ import annotations

import httpx

from redfetch import net
from redfetch import store
from redfetch.sync_discovery import payload_version_id
from redfetch.sync_types import LocalSnapshot, UpdateCheckResult


def _count_outdated(
    local_snapshot: LocalSnapshot,
    manifest: dict,
    caller_resource_id: str | None = None,
) -> UpdateCheckResult:
    manifest_resources = manifest.get("resources") or {}

    updates_available = 0
    caller_found = False
    caller_outdated = False

    for local_state in local_snapshot.install_targets.values():
        manifest_entry = manifest_resources.get(local_state.resource_id)
        remote_version = payload_version_id(manifest_entry)
        if local_state.version_local is None or remote_version is None:
            continue
        is_caller_root = (
            caller_resource_id
            and local_state.resource_id == caller_resource_id
            and local_state.target_kind == "root"
        )
        if local_state.version_local != remote_version:
            updates_available += 1
            if is_caller_root:
                caller_outdated = True
        if is_caller_root:
            caller_found = True

    if caller_resource_id and caller_found:
        caller_update_available = caller_outdated
    else:
        caller_update_available = None

    return UpdateCheckResult(
        updates_available=updates_available,
        caller_update_available=caller_update_available,
        caller_resource_id=caller_resource_id,
    )


async def check_for_updates(
    db_path: str,
    headers: dict,
    caller_resource_id: str | None = None,
) -> UpdateCheckResult:
    """Compare local DB versions against the manifest"""
    local_snapshot = await store.load_local_snapshot(db_path)

    async with httpx.AsyncClient(
        headers=headers,
        http2=True,
        timeout=30.0,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        manifest = await net.fetch_manifest_cached(client)

    return _count_outdated(local_snapshot, manifest, caller_resource_id)
