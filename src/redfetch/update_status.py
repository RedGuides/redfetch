"""Writes update_status.json after `redfetch check` completes."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal

from redfetch import config
from redfetch.sync_types import ExecutionPlan, ExecutionResult, PlannedAction

SCHEMA_VERSION = 1
UPDATE_STATUS_FILENAME = "update_status.json"

AuthState = Literal["ok", "needs_login", "not_configured"]


def update_status_path() -> str:
    """Location beside last_command.json so external apps need only one path."""
    return os.path.join(config.DEFAULT_CONFIG_DIR, UPDATE_STATUS_FILENAME)


def _is_outdated_download(action: PlannedAction) -> bool:
    """Blocked items are excluded on purpose"""
    return action.action == "download" and action.reason == "outdated"


def _item(action: PlannedAction) -> dict:
    return {
        "resource_id": action.resource_id,
        "name": action.title or action.resource_id,
        "available_version_id": action.remote_version,
        "version": action.remote_version_string,
    }


def build_items_from_plan(execution_plan: ExecutionPlan) -> list[dict]:
    """Only stuff we're actually going to download (opt-outs, blocks, and new installs aren't in the plan)."""
    return [_item(a) for a in execution_plan.actions.values() if _is_outdated_download(a)]


def split_items_by_outcome(
    execution_plan: ExecutionPlan,
    execution_result: ExecutionResult,
) -> tuple[list[dict], list[dict]]:
    """Split the planned updates into (installed, remaining) by what actually downloaded."""
    installed: dict[str, dict] = {}
    remaining: dict[str, dict] = {}
    for action in execution_plan.actions.values():
        if not _is_outdated_download(action):
            continue
        item = _item(action)
        result = execution_result.items.get(action.target_key)
        outcome = result.outcome if result is not None else None
        if outcome == "downloaded":
            installed.setdefault(action.resource_id, item)
        elif outcome == "skipped":
            pass  # already up to date: goes in neither list
        else:
            remaining.setdefault(action.resource_id, item)
    # A resource with any failed target is still outdated, not installed.
    for resource_id in remaining:
        installed.pop(resource_id, None)
    return list(installed.values()), list(remaining.values())


def write_update_status(
    *,
    env: str,
    auth_state: AuthState,
    items: list[dict] | None = None,
    managed_path: str | None = None,
    auto_update: bool | None = None,
    installed: list[dict] | None = None,
    pending_restart: bool | None = None,
    pending_restart_version: str | None = None,
) -> dict:
    """Write update_status.json and return the payload. Only auth_state "ok" carries items."""
    items = items or []
    if auth_state != "ok":
        items = []

    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": int(datetime.now(timezone.utc).timestamp()),
        "env": env.upper(),
        "auth_state": auth_state,
        "managed_path": managed_path,
        "updates": {
            "items": items,
        },
    }
    if auto_update is not None:
        payload["auto_update"] = bool(auto_update)
    if installed is not None:
        payload["installed"] = installed
    if pending_restart is not None:
        payload["pending_restart"] = bool(pending_restart)
    if pending_restart_version is not None:
        payload["pending_restart_version"] = pending_restart_version
    config.atomic_write_json(update_status_path(), payload)
    return payload
