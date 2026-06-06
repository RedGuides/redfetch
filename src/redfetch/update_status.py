"""Writes update_status.json after `redfetch check` completes."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal

from redfetch import config
from redfetch.sync_types import ExecutionPlan

SCHEMA_VERSION = 1
UPDATE_STATUS_FILENAME = "update_status.json"

AuthState = Literal["ok", "needs_login", "not_configured"]


def update_status_path() -> str:
    """Location beside last_command.json so external apps need only one path."""
    return os.path.join(config.DEFAULT_CONFIG_DIR, UPDATE_STATUS_FILENAME)


def build_items_from_plan(execution_plan: ExecutionPlan) -> list[dict]:
    """Only stuff we're actually going to download (opt-outs, blocks, and new installs aren't in the plan)."""
    items: list[dict] = []
    for action in execution_plan.actions.values():
        if action.action != "download" or action.reason != "outdated":
            continue
        items.append(
            {
                "resource_id": action.resource_id,
                "name": action.title or action.resource_id,
                "available_version_id": action.remote_version,
            }
        )
    return items


def write_update_status(
    *,
    env: str,
    auth_state: AuthState,
    items: list[dict] | None = None,
    managed_path: str | None = None,
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
    config.atomic_write_json(update_status_path(), payload)
    return payload
