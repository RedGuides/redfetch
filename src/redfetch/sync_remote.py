from __future__ import annotations

from typing import NamedTuple

import httpx

from redfetch import api
from redfetch import net
from redfetch.sync_discovery import payload_category_id, payload_title, payload_version_id
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    LocalSnapshot,
    RemoteArtifact,
    RemoteResourceState,
    RemoteSnapshot,
)


def _normalize_hash(raw_hash: str | None) -> str | None:
    """Sanitize the xenforo file hash to a standard format."""
    if not raw_hash:
        return None
    cleaned = str(raw_hash).strip().lower()
    if len(cleaned) == 32 and all(char in "0123456789abcdef" for char in cleaned):
        return cleaned
    return None


class _PayloadDetails(NamedTuple):
    """Bundle of fields extracted from the API for building a RemoteResourceState."""
    title: str | None
    category_id: int | None
    version_id: int | None
    artifact: RemoteArtifact | None


def _extract_artifact(payload: dict) -> RemoteArtifact | None:
    """Extract downloadable file info."""
    current_files = payload.get("current_files")
    if not isinstance(current_files, list) or len(current_files) != 1:
        return None
    file_info = current_files[0]
    if file_info.get("id") is None or not file_info.get("filename") or not file_info.get("download_url"):
        return None
    return RemoteArtifact(
        file_id=int(file_info["id"]),
        filename=str(file_info["filename"]),
        download_url=str(file_info["download_url"]),
        file_hash=_normalize_hash(file_info.get("hash")),
    )


def _payload_details(payload: dict | None) -> _PayloadDetails:
    """Bundle all fields extracted from a raw API payload."""
    if not payload:
        return _PayloadDetails(None, None, None, None)
    return _PayloadDetails(
        title=payload_title(payload),
        category_id=payload_category_id(payload),
        version_id=payload_version_id(payload),
        artifact=_extract_artifact(payload),
    )


def _needs_live_check(
    *,
    desired_targets: list[DesiredInstallTarget],
    local_snapshot: LocalSnapshot,
    manifest_details: _PayloadDetails | None,
) -> bool:
    """Return True if the resource needs an update, or data is incomplete, or config has changed."""
    if manifest_details is None or manifest_details.version_id is None or manifest_details.artifact is None:
        return True

    for target in desired_targets:
        local_state = local_snapshot.install_targets.get(target.target_key)
        if local_state is None:
            return True
        if local_state.version_local is None:
            return True
        if local_state.version_local != manifest_details.version_id:
            return True
        if (
            local_state.resolved_path != target.resolved_path
            or local_state.flatten != target.flatten
            or local_state.protected_files != target.protected_files
        ):
            return True

    return False


def _blocked_state(
    resource_id: str,
    *,
    status: str,
    manifest_details: _PayloadDetails | None,
    live_title: str | None = None,
    live_category_id: int | None = None,
) -> RemoteResourceState:
    """Build a RemoteResourceState for a resource that is blocked."""
    return RemoteResourceState(
        resource_id=resource_id,
        title=(manifest_details.title if manifest_details else None) or live_title,
        category_id=(manifest_details.category_id if manifest_details and manifest_details.category_id is not None else live_category_id),
        version_id=manifest_details.version_id if manifest_details else None,
        status=status,
        artifact=None,
        source_note="manifest" if manifest_details else "live_access_only",
    )


async def fetch_remote_snapshot(
    *,
    client: httpx.AsyncClient,
    desired_set: DesiredSet,
    local_snapshot: LocalSnapshot,
) -> RemoteSnapshot:
    """Assemble the remote half of the sync picture: version, status, and download info per resource."""
    manifest = await net.fetch_manifest_cached(client)
    manifest_resources = manifest.get("resources", {}) or {}

    remote_resources: dict[str, RemoteResourceState] = {}
    ids_needing_live_check: list[str] = []
    manifest_cache: dict[str, _PayloadDetails | None] = {}

    for resource_id in desired_set.resource_ids:
        manifest_entry = manifest_resources.get(resource_id)
        manifest_details = _payload_details(manifest_entry) if manifest_entry else None
        manifest_cache[resource_id] = manifest_details
        desired_targets = desired_set.resource_targets(resource_id)
        if _needs_live_check(
            desired_targets=desired_targets,
            local_snapshot=local_snapshot,
            manifest_details=manifest_details,
        ):
            ids_needing_live_check.append(resource_id)
            continue

        remote_resources[resource_id] = RemoteResourceState(
            resource_id=resource_id,
            title=manifest_details.title,
            category_id=manifest_details.category_id,
            version_id=manifest_details.version_id,
            status="manifest_current",
            artifact=manifest_details.artifact,
            source_note="manifest_only",
        )

    if ids_needing_live_check:
        live_records = await api.fetch_resource_records_batch(client, ids_needing_live_check)
        for record in live_records:
            resource_id = record.resource_id
            manifest_details = manifest_cache.get(resource_id)
            live_title = payload_title(record.resource)
            live_category_id = payload_category_id(record.resource)

            if record.status != "downloadable":
                remote_resources[resource_id] = _blocked_state(
                    resource_id,
                    status=record.status,
                    manifest_details=manifest_details,
                    live_title=live_title,
                    live_category_id=live_category_id,
                )
                continue

            remote_resources[resource_id] = RemoteResourceState(
                resource_id=resource_id,
                title=(manifest_details.title if manifest_details else None) or live_title,
                category_id=(manifest_details.category_id if manifest_details and manifest_details.category_id is not None else live_category_id),
                version_id=manifest_details.version_id if manifest_details else None,
                status="downloadable",
                artifact=manifest_details.artifact if manifest_details else None,
                source_note="manifest_plus_access_check" if manifest_details else "live_access_only",
            )

    return RemoteSnapshot(resources=remote_resources)
