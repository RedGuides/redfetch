from __future__ import annotations

import asyncio

import httpx

from redfetch import config
from redfetch import store
from redfetch import sync_discovery
from redfetch import sync_executor
from redfetch import sync_planner
from redfetch import sync_remote
from redfetch.sync_types import ExecutionPlan, ExecutionResult, SyncEventCallback


_sync_lock: asyncio.Lock | None = None


def _print_plan_summary(execution_plan: ExecutionPlan) -> None:
    counts = execution_plan.action_counts()
    print(f"Resources in scope: >>> {len(execution_plan.actions)} <<<")
    print(f"Resources to download: >>> {counts.get('download', 0)} <<<")
    if counts.get("block", 0):
        print(f"Resources blocked: >>> {counts.get('block', 0)} <<<")
    if counts.get("untrack", 0):
        print(f"Resources to untrack: >>> {counts.get('untrack', 0)} <<<")


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

    _print_plan_summary(execution_plan)
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
        errored_resources = [
            item.resource_id
            for item in execution_result.items.values()
            if item.outcome == "error"
        ]
        if errored_resources:
            print("One or more resources failed to download.")
            print(f"Failed resources: {errored_resources}")
    elif resource_ids is not None and not success:
        print(
            f"No valid resources found for IDs: {resource_ids}. "
            "Are you in the right server env? Did you opt_in in your settings.local.toml?"
        )
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
