from __future__ import annotations

import asyncio

import httpx

from redfetch import config
from redfetch import store
from redfetch import sync_discovery
from redfetch import sync_executor
from redfetch import sync_planner
from redfetch import sync_remote
from redfetch.sync_types import (
    ExecutionPlan,
    ExecutionResult,
    PLAN_REASON_META,
    ReasonInfo,
    SyncEventCallback,
    reason_message,
)


_sync_lock: asyncio.Lock | None = None
_DEFAULT_REASON = ReasonInfo(message="")


def _print_plan_summary(
    execution_plan: ExecutionPlan,
    resource_ids: list[str] | None = None,
) -> None:
    is_full_sync = resource_ids is None
    all_blocked = [
        action for action in execution_plan.actions.values()
        if action.action == "block"
    ]
    if is_full_sync:
        quiet = [a for a in all_blocked if PLAN_REASON_META.get(a.reason, _DEFAULT_REASON).quiet]
        blocked = [a for a in all_blocked if not PLAN_REASON_META.get(a.reason, _DEFAULT_REASON).quiet]
    else:
        quiet = []
        blocked = all_blocked

    counts = execution_plan.action_counts()
    print(f"Resources in scope: >>> {len(execution_plan.actions)} <<<")
    print(f"Resources to download: >>> {counts.get('download', 0)} <<<")
    if blocked:
        print(f"Resources blocked: >>> {len(blocked)} <<<")
        for action in blocked:
            label = action.title or action.resource_id
            print(f"  - {label} (id={action.resource_id}): {reason_message(action.reason)}")

    summary_buckets: dict[str, int] = {}
    for action in quiet:
        label = PLAN_REASON_META[action.reason].summary_label
        if label:
            summary_buckets[label] = summary_buckets.get(label, 0) + 1
    for label, count in summary_buckets.items():
        print(f"{label}: >>> {count} <<<")

    if counts.get("untrack", 0):
        print(f"Resources to untrack: >>> {counts.get('untrack', 0)} <<<")


def _print_failure_detail(
    execution_plan: ExecutionPlan,
    resource_ids: list[str],
) -> None:
    """Print a hint when a targeted sync fails with zero scoped actions (resource not in scope at all)."""
    requested = {str(rid) for rid in resource_ids}
    has_scoped_actions = any(
        action.root_resource_id in requested
        for action in execution_plan.actions.values()
    )
    if not has_scoped_actions:
        print(
            f"No valid resources found for IDs: {resource_ids}. "
            "Are you in the right server env? Did you opt_in in your settings.local.toml?"
        )


def _run_succeeded(
    *,
    execution_plan: ExecutionPlan,
    execution_result: ExecutionResult,
    resource_ids: list[str] | None,
) -> bool:
    """Targeted sync succeeds when there are no errors, at least one explicit root exists, and no target in the requested closure is blocked."""
    if execution_result.has_errors():
        return False

    if resource_ids is None:
        return True

    requested_root_ids = {str(resource_id) for resource_id in resource_ids}
    scoped_actions = [
        action
        for action in execution_plan.actions.values()
        if action.root_resource_id in requested_root_ids
    ]
    if not scoped_actions:
        return False

    if not any(action.explicit_root for action in scoped_actions):
        return False

    for action in scoped_actions:
        item = execution_result.items.get(action.target_key)
        if item is None or item.outcome == "blocked":
            return False
    return True


async def sync(
    db_path: str,
    headers: dict,
    resource_ids: list[str] | None = None,
    on_event: SyncEventCallback | None = None,
) -> bool:
    """Discover, plan, and execute a sync run against the API."""
    settings_env = config.settings.ENV
    local_snapshot = await store.load_local_snapshot(db_path)

    async with httpx.AsyncClient(
        headers=headers,
        http2=True,
        timeout=30.0,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        desired_set = await sync_discovery.discover_desired_set(
            client=client,
            resource_ids=resource_ids,
            settings_env=settings_env,
        )
        remote_snapshot = await sync_remote.fetch_remote_snapshot(
            client=client,
            desired_set=desired_set,
            local_snapshot=local_snapshot,
        )

    execution_plan = sync_planner.build_execution_plan(
        desired_set=desired_set,
        remote_snapshot=remote_snapshot,
        local_snapshot=local_snapshot,
        settings_env=settings_env,
    )

    _print_plan_summary(execution_plan, resource_ids=resource_ids)
    if on_event:
        on_event(("total", len(execution_plan.actions), None))

    execution_result = await sync_executor.execute_plan(
        headers=headers,
        desired_set=desired_set,
        remote_snapshot=remote_snapshot,
        execution_plan=execution_plan,
        on_event=on_event,
        on_download_success=lambda target, action, remote: store.record_download_success(
            db_path,
            target=target,
            action=action,
            remote_state=remote,
        ),
    )

    try:
        await store.record_installed_state(
            db_path,
            desired_set=desired_set,
            remote_snapshot=remote_snapshot,
            local_snapshot=local_snapshot,
            execution_plan=execution_plan,
            execution_result=execution_result,
        )
    except Exception as exc:
        print(f"Warning: failed to record sync state: {exc}")

    success = _run_succeeded(
        execution_plan=execution_plan,
        execution_result=execution_result,
        resource_ids=resource_ids,
    )

    if execution_result.has_errors():
        errored_items = [
            item
            for item in execution_result.items.values()
            if item.outcome == "error"
        ]
        if errored_items:
            print("One or more resources failed to download.")
            for item in errored_items:
                detail = f": {item.error_detail}" if item.error_detail else ""
                print(f"  - {item.resource_id}{detail}")
    elif resource_ids is not None and not success:
        _print_failure_detail(execution_plan, resource_ids)
    elif any(item.outcome == "downloaded" for item in execution_result.items.values()):
        print("All resources downloaded successfully.")
    else:
        print("All resources are up-to-date; no downloads were necessary.")

    return success


async def run_sync(
    db_path: str,
    headers: dict,
    resource_ids: list[str] | None = None,
    on_event: SyncEventCallback | None = None,
    navmesh_override: bool | None = None,
) -> bool:
    """Top-level entry point: run the sync pipeline under a global lock, then navmesh if applicable."""
    global _sync_lock
    if _sync_lock is None:
        _sync_lock = asyncio.Lock()

    try:
        async with _sync_lock:
            result = await sync(
                db_path,
                headers,
                resource_ids=resource_ids,
                on_event=on_event,
            )

            if resource_ids is None:
                from redfetch import navmesh

                navmesh_ok = await navmesh.sync_navmeshes(
                    db_path,
                    headers,
                    on_event=on_event,
                    override=navmesh_override,
                )
                if not navmesh_ok:
                    print("navmesh sync encountered errors")

            return result
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Download cancelled by user.")
        return False
