"""Fetches info about resources from the website."""

from __future__ import annotations

from typing import NamedTuple

from redfetch.sync_discovery import (
    payload_category_id,
    payload_title,
    payload_version_id,
    payload_version_string,
)
from redfetch.sync_types import (
    DesiredSet,
    RemoteArtifact,
    RemoteResourceState,
    RemoteSnapshot,
    RemoteStatus,
    SyncInfo,
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
    """Manifest data needed to describe a remote resource and choose its status."""
    title: str | None
    category_id: int | None
    version_id: int | None
    version_string: str | None
    artifact: RemoteArtifact | None
    access_tier: str | None
    requires_license: bool
    files_count: int


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


def _payload_details(payload: dict) -> _PayloadDetails:
    """Read the manifest fields used to build a remote resource state."""
    current_files = payload.get("current_files")
    files_count = len(current_files) if isinstance(current_files, list) else 0
    return _PayloadDetails(
        title=payload_title(payload),
        category_id=payload_category_id(payload),
        version_id=payload_version_id(payload),
        version_string=payload_version_string(payload),
        artifact=_extract_artifact(payload),
        access_tier=payload.get("access_tier"),
        requires_license=bool(payload.get("requires_license", False)),
        files_count=files_count,
    )


def resolve_status(
    *,
    resource_id: str,
    access_tier: str | None,
    requires_license: bool,
    files_count: int,
    is_level_2: bool,
    is_moderator: bool,
    licensed: set[str],
) -> RemoteStatus:
    """Return the download status implied by the manifest access fields."""
    if not is_moderator:
        if access_tier == "restricted":
            return "access_denied"
        if access_tier == "level2" and not is_level_2:
            return "needs_level_2"
        if requires_license and resource_id not in licensed:
            return "needs_license"
    if files_count == 0:
        return "no_files"
    if files_count > 1:
        return "multiple_files"
    return "downloadable"


def fetch_remote_snapshot(
    *,
    desired_set: DesiredSet,
    manifest: dict,
    sync_info: SyncInfo,
) -> RemoteSnapshot:
    """Build the remote sync state from the manifest and account permissions."""
    manifest_resources = manifest.get("resources", {}) or {}
    remote_resources: dict[str, RemoteResourceState] = {}

    for resource_id in desired_set.resource_ids:
        manifest_entry = manifest_resources.get(resource_id)
        if manifest_entry is None:
            remote_resources[resource_id] = RemoteResourceState(
                resource_id=resource_id,
                status="not_found",
                source_note="manifest_absent",
            )
            continue

        details = _payload_details(manifest_entry)
        status = resolve_status(
            resource_id=resource_id,
            access_tier=details.access_tier,
            requires_license=details.requires_license,
            files_count=details.files_count,
            is_level_2=sync_info.is_level_2,
            is_moderator=sync_info.is_moderator,
            licensed=sync_info.licensed_ids,
        )
        remote_resources[resource_id] = RemoteResourceState(
            resource_id=resource_id,
            title=details.title,
            category_id=details.category_id,
            version_id=details.version_id,
            version_string=details.version_string,
            status=status,
            artifact=details.artifact if status == "downloadable" else None,
            source_note="manifest",
        )

    return RemoteSnapshot(resources=remote_resources)
