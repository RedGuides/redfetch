from __future__ import annotations

from redfetch import config
from redfetch.sync_discovery import (
    is_special_resource,
    resolve_dependency_path,
    resolve_root_path,
)
from redfetch.sync_types import (
    ActionType,
    DesiredInstallTarget,
    DesiredSet,
    ExecutionPlan,
    LocalInstallState,
    LocalSnapshot,
    PlannedAction,
    PlanReason,
    RemoteResourceState,
    RemoteSnapshot,
    parse_target_key,
    target_depth,
)


BLOCKING_STATUSES = {"access_denied", "no_files", "multiple_files", "not_found", "fetch_error"}


def _desired_targets_in_order(desired_set: DesiredSet) -> list[DesiredInstallTarget]:
    """Sort targets so parents are always planned before their children."""
    return sorted(
        desired_set.install_targets.values(),
        key=lambda target: (target_depth(target.target_key), target.target_key),
    )


def _cycle_block_keys(desired_set: DesiredSet) -> set[str]:
    """Block targets that would cause circular dependency loops."""
    cycle_keys: set[str] = set()
    for target in desired_set.install_targets.values():
        segments = parse_target_key(target.target_key)
        if len(segments) == len(set(segments)):
            continue
        repeated_resource_id = segments[-1]
        first_index = segments[:-1].index(repeated_resource_id)
        first_blocked_prefix = first_index + 2
        for prefix_length in range(first_blocked_prefix, len(segments) + 1):
            candidate_key = f"/{'/'.join(segments[:prefix_length])}/"
            if candidate_key in desired_set.install_targets:
                cycle_keys.add(candidate_key)
    return cycle_keys


def _resolve_target_path(
    target: DesiredInstallTarget,
    *,
    parent_action: PlannedAction | None,
    remote_state: RemoteResourceState | None,
    settings_env: str,
) -> tuple[str | None, str | None]:
    """Figure out where on disk this target should be installed."""
    if target.resolved_path is not None:
        return target.resolved_path, target.subfolder

    if target.target_kind == "root":
        category_id = target.category_id
        if category_id is None and remote_state is not None:
            category_id = remote_state.category_id
        if not is_special_resource(target.resource_id, settings_env):
            if category_id is None or category_id not in config.CATEGORY_MAP:
                return None, target.subfolder
        return resolve_root_path(target.resource_id, category_id, settings_env), None

    if parent_action is None or not parent_action.resolved_path or target.parent_id is None:
        return None, target.subfolder
    resolved_path, subfolder = resolve_dependency_path(
        parent_action.resolved_path,
        target.parent_id,
        target.resource_id,
        settings_env,
    )
    return resolved_path, (target.subfolder or subfolder)


def _install_context_changed(
    local_state: LocalInstallState | None,
    *,
    resolved_path: str | None,
    subfolder: str | None,
    target: DesiredInstallTarget,
) -> bool:
    """Check if path, subfolder, or install settings have changed since the last sync."""
    if local_state is None:
        return False
    return (
        local_state.resolved_path != resolved_path
        or local_state.subfolder != subfolder
        or local_state.flatten != target.flatten
        or local_state.protected_files != target.protected_files
    )


def _decide_action(
    target: DesiredInstallTarget,
    *,
    local_state: LocalInstallState | None,
    remote_state: RemoteResourceState | None,
    parent_action: PlannedAction | None,
    cycle_block_keys: set[str],
    resolved_path: str | None,
    subfolder: str | None,
) -> tuple[ActionType, PlanReason]:
    """Decide whether a single target should be downloaded, skipped, or blocked, and why."""
    if target.target_key in cycle_block_keys:
        return "block", "dependency_cycle"
    if target.parent_target_key and (
        parent_action is None or parent_action.action in {"block", "untrack"}
    ):
        return "block", "parent_blocked"
    if remote_state is None:
        return "block", "fetch_error"
    if remote_state.status in BLOCKING_STATUSES:
        return "block", remote_state.status  # type: ignore[return-value]
    if resolved_path is None and target.target_kind == "root":
        return "block", "unknown_category"
    if _install_context_changed(
        local_state, resolved_path=resolved_path, subfolder=subfolder, target=target,
    ):
        return "download", "install_context_changed"
    if (
        local_state is not None
        and remote_state.version_id is not None
        and local_state.version_local == remote_state.version_id
    ):
        return "skip", "already_current"
    if local_state is None or local_state.version_local is None:
        return "download", "not_installed"
    return "download", "outdated"


def build_execution_plan(
    *,
    desired_set: DesiredSet,
    remote_snapshot: RemoteSnapshot,
    local_snapshot: LocalSnapshot,
    settings_env: str,
) -> ExecutionPlan:
    """Main planner entry point: decide the action for every target, then mark untracked leftovers."""
    actions: dict[str, PlannedAction] = {}
    cycle_keys = _cycle_block_keys(desired_set)

    for target in _desired_targets_in_order(desired_set):
        local_state = local_snapshot.install_targets.get(target.target_key)
        remote_state = remote_snapshot.resources.get(target.resource_id)
        parent_action = actions.get(target.parent_target_key) if target.parent_target_key else None
        resolved_path, subfolder = _resolve_target_path(
            target,
            parent_action=parent_action,
            remote_state=remote_state,
            settings_env=settings_env,
        )
        action, reason = _decide_action(
            target,
            local_state=local_state,
            remote_state=remote_state,
            parent_action=parent_action,
            cycle_block_keys=cycle_keys,
            resolved_path=resolved_path,
            subfolder=subfolder,
        )
        actions[target.target_key] = PlannedAction.from_desired(
            target,
            action=action,
            reason=reason,
            title=remote_state.title if remote_state else target.title,
            category_id=remote_state.category_id if remote_state else target.category_id,
            remote_version=remote_state.version_id if remote_state else None,
            artifact=remote_state.artifact if remote_state and action == "download" else None,
            resolved_path=resolved_path,
            subfolder=subfolder,
        )

    if desired_set.mode == "full":
        local_scope = local_snapshot.install_targets.values()
    else:
        local_scope = local_snapshot.roots_in_closure(desired_set.requested_root_ids)

    for local_state in local_scope:
        if local_state.target_key in actions:
            continue
        actions[local_state.target_key] = PlannedAction.untrack_from_local(local_state)

    return ExecutionPlan(actions=actions)
