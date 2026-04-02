from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from redfetch import download
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    ExecutionPlan,
    ExecutionResult,
    ExecutionResultItem,
    PlannedAction,
    RemoteResourceState,
    RemoteSnapshot,
)

DOWNLOAD_CONCURRENCY = 6

_INSTANT_OUTCOMES = {"skip": "skipped", "block": "blocked", "untrack": "untracked"}

DownloadSuccessHook = Callable[
    [DesiredInstallTarget, PlannedAction, RemoteResourceState], Awaitable[None]
]


def _make_item(
    action: PlannedAction,
    outcome: str,
    *,
    reason: str | None = None,
    version: str | None = None,
    error: str | None = None,
) -> ExecutionResultItem:
    """Fill out a result record for the resource. Auto-copies identity fields so callers only pass what varies."""
    return ExecutionResultItem(
        target_key=action.target_key,
        resource_id=action.resource_id,
        outcome=outcome,
        reason=reason or action.reason,
        written_version=version,
        error_detail=error,
    )


async def _do_download(
    client: httpx.AsyncClient, action: PlannedAction,
) -> tuple[bool, str | None]:
    """Perform one download attempt, returning success and any error."""
    try:
        ok = await download.download_install_target_async(
            client=client,
            resource_id=action.resource_id,
            download_url=action.artifact.download_url,
            filename=action.artifact.filename,
            file_hash=action.artifact.file_hash,
            folder_path=action.resolved_path,
            should_flatten=action.flatten,
            protected_files=action.protected_files,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return False, str(exc)
    return ok, (None if ok else "download failed")


async def execute_plan(
    *,
    headers: dict,
    desired_set: DesiredSet,
    remote_snapshot: RemoteSnapshot,
    execution_plan: ExecutionPlan,
    on_event: Callable | None = None,
    on_download_success: DownloadSuccessHook | None = None,
) -> ExecutionResult:
    """Execute planned actions (skips, downloads, etc.) and build the result report."""
    result = ExecutionResult(items={})
    satisfied: set[str] = set()

    def emit(kind: str, resource_id: int, detail: str) -> None:
        if on_event:
            on_event((kind, resource_id, detail))

    for action in execution_plan.actions.values():
        outcome = _INSTANT_OUTCOMES.get(action.action)
        if outcome is None:
            continue
        version = action.remote_version if action.action == "skip" else None
        result.items[action.target_key] = _make_item(action, outcome, version=version)
        if action.action == "skip":
            satisfied.add(action.target_key)
        emit("done", action.resource_id, outcome)

    download_actions = {
        k: a for k, a in execution_plan.actions.items() if a.action == "download"
    }
    if not download_actions:
        return result

    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    gate: dict[str, asyncio.Event] = {k: asyncio.Event() for k in download_actions}

    async def run_one(key: str, client: httpx.AsyncClient) -> None:
        action = download_actions[key]
        parent = action.parent_target_key
        try:
            if parent and parent in gate:
                await gate[parent].wait()

            if parent and parent not in satisfied:
                result.items[key] = _make_item(action, "blocked", reason="parent_failed")
                emit("done", action.resource_id, "blocked")
                return

            if action.artifact is None or action.resolved_path is None:
                result.items[key] = _make_item(
                    action, "error", error="missing artifact or resolved path",
                )
                emit("done", action.resource_id, "error")
                return

            async with sem:
                emit("start", action.resource_id, action.title)
                ok, error_detail = await _do_download(client, action)

            if ok:
                result.items[key] = _make_item(
                    action, "downloaded", version=action.remote_version,
                )
                satisfied.add(key)
                if on_download_success:
                    await on_download_success(
                        desired_set.install_targets[key],
                        action,
                        remote_snapshot.resources[action.resource_id],
                    )
                emit("done", action.resource_id, "downloaded")
            else:
                result.items[key] = _make_item(action, "error", error=error_detail)
                emit("done", action.resource_id, "error")
        finally:
            gate[key].set()

    async with httpx.AsyncClient(
        headers=headers,
        http2=True,
        timeout=60.0,
        limits=httpx.Limits(
            max_connections=DOWNLOAD_CONCURRENCY,
            max_keepalive_connections=DOWNLOAD_CONCURRENCY,
        ),
    ) as client:
        await asyncio.gather(*(run_one(k, client) for k in download_actions))

    return result
