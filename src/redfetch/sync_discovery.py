from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from redfetch import api
from redfetch import config
from redfetch import utils
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    make_child_target_key,
    make_root_target_key,
    parse_target_key,
)


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------

def is_special_resource(resource_id: str, settings_env: str) -> bool:
    """Check if a resource is configured in SPECIAL_RESOURCES for the given environment."""
    return str(resource_id) in config.settings.from_env(settings_env).SPECIAL_RESOURCES


def _get_dependency_details(parent_id: str, resource_id: str, settings_env: str) -> dict[str, Any]:
    settings_for_env = config.settings.from_env(settings_env)
    parent_settings = settings_for_env.SPECIAL_RESOURCES.get(str(parent_id), {})
    dependencies = parent_settings.get("dependencies", {})
    return dependencies.get(str(resource_id), {})


def resolve_dependency_path(parent_target_path: str, parent_id: str, resource_id: str, settings_env: str) -> tuple[str, str | None]:
    dependency_details = _get_dependency_details(parent_id, resource_id, settings_env)
    subfolder = dependency_details.get("subfolder", "") or ""
    resolved_path = os.path.join(parent_target_path, subfolder) if subfolder else parent_target_path
    return os.path.normpath(resolved_path), (subfolder or None)


def _get_protected_files(resource_id: str, settings_env: str) -> list[str]:
    settings_for_env = config.settings.from_env(settings_env)
    protected = getattr(settings_for_env, "PROTECTED_FILES_BY_RESOURCE", {})
    return list(protected.get(str(resource_id), []))


def resolve_root_path(resource_id: str, category_id: int | None, settings_env: str) -> str:
    settings_for_env = config.settings.from_env(settings_env)
    download_folder = os.path.normpath(settings_for_env.DOWNLOAD_FOLDER) if settings_for_env.DOWNLOAD_FOLDER else ""

    special_resource = settings_for_env.SPECIAL_RESOURCES.get(str(resource_id))
    special_destination = utils.resolve_special_destination(special_resource, download_folder)
    if special_destination:
        return special_destination

    category_name = config.CATEGORY_MAP.get(category_id or -1, "")

    if category_name:
        category_paths = getattr(settings_for_env, "CATEGORY_PATHS", None) or {}
        override = category_paths.get(category_name)
        if override:
            if os.path.isabs(override):
                return os.path.normpath(override)
            return os.path.normpath(os.path.join(download_folder, override))

    base_path = download_folder
    vvmq_id = utils.get_current_vvmq_id(settings_env)
    if vvmq_id:
        vvmq_resource = settings_for_env.SPECIAL_RESOURCES.get(vvmq_id)
        vvmq_destination = utils.resolve_special_destination(vvmq_resource, download_folder)
        if vvmq_destination:
            base_path = vvmq_destination

    if category_name:
        return os.path.normpath(os.path.join(base_path, category_name))
    return base_path


def _get_flatten(resource_id: str, parent_id: str | None, settings_env: str) -> bool:
    if parent_id:
        dependency_details = _get_dependency_details(parent_id, resource_id, settings_env)
        if "flatten" in dependency_details:
            return bool(dependency_details["flatten"])

    settings_for_env = config.settings.from_env(settings_env)
    special_resource = settings_for_env.SPECIAL_RESOURCES.get(str(resource_id), {})
    return bool(special_resource.get("flatten", False))


# ---------------------------------------------------------------------------
# Discovery logic
# ---------------------------------------------------------------------------

@dataclass
class _RootSpec:
    """Tracks why a resource was selected for sync and its raw API payload."""
    sources: set[str] = field(default_factory=set)
    payload: dict | None = None
    discovery_block: str | None = None


def payload_title(payload: dict | None) -> str | None:
    if not payload:
        return None
    title = payload.get("title")
    return str(title) if title is not None else None


def payload_category_id(payload: dict | None) -> int | None:
    if not payload:
        return None
    category = payload.get("Category") or payload.get("category") or {}
    raw = category.get("parent_category_id") or payload.get("parent_category_id")
    if raw is None:
        return None
    return int(raw)


def payload_version_id(payload: dict | None) -> int | None:
    """Only the manifest endpoint provides this; the live XenForo API does not."""
    if not payload:
        return None
    raw = payload.get("version_id")
    return int(raw) if raw is not None else None


def _category_allowed_in_env(category_id: int | None, settings_env: str) -> bool:
    """Category 11 (plugins) is excluded from TEST and EMU for now."""
    if category_id is None:
        return False
    if category_id not in config.CATEGORY_MAP:
        return False
    if category_id == 11 and settings_env.upper() in {"TEST", "EMU"}:
        return False
    return True


def _root_sources_for_full_sync(
    watched_resources: list[dict],
    licenses: list[dict],
    settings_env: str,
) -> dict[str, _RootSpec]:
    """Collect every resource that qualifies for a full sync and record how each one qualified."""
    specs: dict[str, _RootSpec] = {}

    for payload in watched_resources:
        category_id = payload_category_id(payload)
        if not _category_allowed_in_env(category_id, settings_env):
            continue
        resource_id = str(payload["resource_id"])
        spec = specs.setdefault(resource_id, _RootSpec())
        spec.sources.add("watching")
        spec.payload = payload

    for license_info in licenses:
        if not license_info.get("active", False):
            continue
        end_date = license_info.get("end_date", 0)
        is_expired = end_date != 0 and end_date < time.time()
        payload = license_info.get("resource") or {}
        category_id = payload_category_id(payload)
        if not _category_allowed_in_env(category_id, settings_env):
            continue
        resource_id = str(payload["resource_id"])
        spec = specs.setdefault(resource_id, _RootSpec())
        spec.sources.add("licensed")
        if is_expired:
            spec.discovery_block = "license_expired"
        spec.payload = payload

    settings_for_env = config.settings.from_env(settings_env)
    for resource_id, resource_info in settings_for_env.SPECIAL_RESOURCES.items():
        if resource_info.get("opt_in", False):
            specs.setdefault(str(resource_id), _RootSpec()).sources.add("special")

    return specs


def _root_sources_for_targeted(resource_ids: list[str], settings_env: str) -> dict[str, _RootSpec]:
    """builds the resource list for a single resource sync, since they can have dependencies."""
    settings_for_env = config.settings.from_env(settings_env)
    specs: dict[str, _RootSpec] = {}
    for resource_id in resource_ids:
        rid = str(resource_id)
        spec = _RootSpec(sources={"explicit"})
        resource_info = settings_for_env.SPECIAL_RESOURCES.get(rid, {})
        if resource_info.get("opt_in", False):
            spec.sources.add("special")
        specs[rid] = spec
    return specs


def _add_root_target(
    desired_set: DesiredSet,
    *,
    resource_id: str,
    sources: set[str],
    payload: dict | None,
    settings_env: str,
) -> DesiredInstallTarget:
    """Resolve path, category, flatten, and protected files for a root resource and add it to the plan."""
    category_id = payload_category_id(payload)
    resolved_path = None
    settings_for_env = config.settings.from_env(settings_env)
    special_resource = settings_for_env.SPECIAL_RESOURCES.get(str(resource_id))
    has_own_destination = bool(
        special_resource and (special_resource.get("custom_path") or special_resource.get("default_path"))
    )
    if has_own_destination or category_id is not None:
        resolved_path = resolve_root_path(resource_id, category_id, settings_env)
    target = DesiredInstallTarget(
        target_key=make_root_target_key(resource_id),
        resource_id=resource_id,
        parent_id=None,
        parent_target_key=None,
        root_resource_id=resource_id,
        target_kind="root",
        sources=set(sources),
        title=payload_title(payload),
        category_id=category_id,
        resolved_path=resolved_path,
        subfolder=None,
        flatten=_get_flatten(resource_id, None, settings_env),
        protected_files=_get_protected_files(resource_id, settings_env),
        explicit_root="explicit" in sources,
    )
    return desired_set.add_target(target)


def _add_dependency_target(
    desired_set: DesiredSet,
    *,
    parent_target: DesiredInstallTarget,
    resource_id: str,
    settings_env: str,
) -> DesiredInstallTarget:
    """Build an install target for a dependency, deriving its path from the parent target."""
    if parent_target.resolved_path:
        resolved_path, subfolder = resolve_dependency_path(
            parent_target.resolved_path, parent_target.resource_id, resource_id, settings_env,
        )
    else:
        resolved_path, subfolder = None, None

    target = DesiredInstallTarget(
        target_key=make_child_target_key(parent_target.target_key, resource_id),
        resource_id=resource_id,
        parent_id=parent_target.resource_id,
        parent_target_key=parent_target.target_key,
        root_resource_id=parent_target.root_resource_id,
        target_kind="dependency",
        sources={"dependency"},
        title=None,
        category_id=None,
        resolved_path=resolved_path,
        subfolder=subfolder,
        flatten=_get_flatten(resource_id, parent_target.resource_id, settings_env),
        protected_files=_get_protected_files(resource_id, settings_env),
        explicit_root=False,
    )
    return desired_set.add_target(target)


def _expand_dependencies(
    desired_set: DesiredSet,
    *,
    parent_target: DesiredInstallTarget,
    settings_env: str,
) -> None:
    """Recursively add all opt-in dependencies of a parent target to the desired set."""
    settings_for_env = config.settings.from_env(settings_env)
    parent_settings = settings_for_env.SPECIAL_RESOURCES.get(parent_target.resource_id, {})
    dependencies = parent_settings.get("dependencies", {})

    for dependency_id, dependency_info in dependencies.items():
        info = dependency_info or {}
        if not info.get("opt_in", False):
            continue
        dependency_id_str = str(dependency_id)
        child_target = _add_dependency_target(
            desired_set,
            parent_target=parent_target,
            resource_id=dependency_id_str,
            settings_env=settings_env,
        )
        if dependency_id_str in parse_target_key(parent_target.target_key):
            # Keep the repeated edge once so planning can block the cycle explicitly.
            continue
        _expand_dependencies(
            desired_set,
            parent_target=child_target,
            settings_env=settings_env,
        )


async def discover_desired_set(
    *,
    client: httpx.AsyncClient,
    resource_ids: list[str] | None,
    settings_env: str,
) -> DesiredSet:
    """Main entry point for discovery: figure out everything that needs to be installed and where it goes."""
    if resource_ids is None:
        watched_resources, licenses = await asyncio.gather(
            api.fetch_watched_resources(client),
            api.fetch_licenses(client),
        )
        root_specs = _root_sources_for_full_sync(watched_resources, licenses, settings_env)
        desired_set = DesiredSet(mode="full")
    else:
        normalized_ids = [str(resource_id) for resource_id in resource_ids]
        root_specs = _root_sources_for_targeted(normalized_ids, settings_env)
        desired_set = DesiredSet(
            mode="targeted",
            requested_root_ids=set(normalized_ids),
        )

    for resource_id, spec in root_specs.items():
        root_target = _add_root_target(
            desired_set,
            resource_id=resource_id,
            sources=spec.sources,
            payload=spec.payload,
            settings_env=settings_env,
        )
        if spec.discovery_block:
            root_target.discovery_block = spec.discovery_block
        _expand_dependencies(
            desired_set,
            parent_target=root_target,
            settings_env=settings_env,
        )

    return desired_set
