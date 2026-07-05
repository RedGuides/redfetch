"""Tests for the non-interactive `check` path: building the toast list from a plan
and writing the update_status.json file contract."""

import json

import pytest

from redfetch import config, update_status
from redfetch.sync_types import ExecutionPlan, PlannedAction


def _action(
    resource_id,
    *,
    action,
    reason,
    title=None,
    remote_version=None,
    target_kind="root",
    parent_id=None,
    root_resource_id=None,
):
    if target_kind == "root":
        target_key = f"/{resource_id}/"
    else:
        target_key = f"/{parent_id}/{resource_id}/"
    return PlannedAction(
        target_key=target_key,
        resource_id=resource_id,
        parent_id=parent_id,
        parent_target_key=f"/{parent_id}/" if parent_id else None,
        root_resource_id=root_resource_id or resource_id,
        target_kind=target_kind,
        action=action,
        reason=reason,
        title=title,
        remote_version=remote_version,
    )


def _plan(*actions):
    return ExecutionPlan(actions={a.target_key: a for a in actions})


def test_build_items_includes_only_outdated_downloads():
    plan = _plan(
        _action("4", action="download", reason="outdated", title="KissAssist", remote_version=1240),
        _action("3040", action="download", reason="outdated", title="RGMercs", remote_version=991),
        # Excluded: a brand-new install is not an "update" to something already held.
        _action("9", action="download", reason="not_installed", title="New Thing", remote_version=5),
        # Excluded: already current.
        _action("10", action="skip", reason="already_current", title="Current", remote_version=7),
        # Excluded: opted out / no longer desired.
        _action("11", action="untrack", reason="not_desired", title="Dropped"),
        # Excluded: blocked (e.g. needs a license / access denied).
        _action("12", action="block", reason="needs_license", title="Lapsed"),
    )

    items = update_status.build_items_from_plan(plan)

    assert items == [
        {"resource_id": "4", "name": "KissAssist", "available_version_id": 1240},
        {"resource_id": "3040", "name": "RGMercs", "available_version_id": 991},
    ]


def test_build_items_falls_back_to_resource_id_when_no_title():
    plan = _plan(_action("4", action="download", reason="outdated", remote_version=1240))
    items = update_status.build_items_from_plan(plan)
    assert items == [{"resource_id": "4", "name": "4", "available_version_id": 1240}]


def test_install_context_changed_is_not_an_update():
    """Re-downloads for path/settings changes aren't user-facing 'updates'."""
    plan = _plan(
        _action("4", action="download", reason="install_context_changed", title="KissAssist", remote_version=1240),
    )
    assert update_status.build_items_from_plan(plan) == []


@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _read_status(status_dir):
    path = status_dir / update_status.UPDATE_STATUS_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_status_ok_with_items(status_dir):
    items = [{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}]
    payload = update_status.write_update_status(env="live", auth_state="ok", items=items)

    on_disk = _read_status(status_dir)
    assert on_disk == payload
    assert on_disk["schema_version"] == update_status.SCHEMA_VERSION
    assert on_disk["env"] == "LIVE"  # uppercased
    assert on_disk["auth_state"] == "ok"
    assert on_disk["updates"]["items"] == items
    assert isinstance(on_disk["checked_at"], int)
    assert on_disk["checked_at"] > 0


def test_write_status_includes_managed_path(status_dir):
    """MQ reads managed_path to ignore stray MQ copies; it must round-trip verbatim."""
    update_status.write_update_status(
        env="LIVE",
        auth_state="ok",
        items=[{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}],
        managed_path=r"D:\EverQuest\VanillaMQ_LIVE",
    )
    assert _read_status(status_dir)["managed_path"] == r"D:\EverQuest\VanillaMQ_LIVE"


def test_managed_path_defaults_to_null(status_dir):
    """Not_configured / unresolved trees write an explicit null so MQ won't gate."""
    update_status.write_update_status(env="EMU", auth_state="not_configured")
    assert _read_status(status_dir)["managed_path"] is None


def test_non_ok_states_force_empty_updates(status_dir):
    update_status.write_update_status(
        env="TEST",
        auth_state="needs_login",
        items=[{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}],
    )
    on_disk = _read_status(status_dir)
    assert on_disk["auth_state"] == "needs_login"
    assert on_disk["updates"]["items"] == []


def test_write_is_atomic_no_leftover_tmp(status_dir):
    update_status.write_update_status(env="EMU", auth_state="not_configured")
    files = {p.name for p in status_dir.iterdir()}
    assert update_status.UPDATE_STATUS_FILENAME in files
    assert not any(name.endswith(".tmp") for name in files)


def test_unicode_titles_survive_round_trip(status_dir):
    items = [{"resource_id": "1", "name": "Café Münster 日本語", "available_version_id": 1}]
    update_status.write_update_status(env="LIVE", auth_state="ok", items=items)
    assert _read_status(status_dir)["updates"]["items"][0]["name"] == "Café Münster 日本語"
