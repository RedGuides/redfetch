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
    remote_version_string=None,
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
        remote_version_string=remote_version_string,
    )


def _plan(*actions):
    return ExecutionPlan(actions={a.target_key: a for a in actions})


def test_build_items_includes_only_outdated_downloads():
    plan = _plan(
        _action("4", action="download", reason="outdated", title="KissAssist", remote_version=1240, remote_version_string="11.005"),
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
        {"resource_id": "4", "name": "KissAssist", "available_version_id": 1240, "version": "11.005"},
        {"resource_id": "3040", "name": "RGMercs", "available_version_id": 991, "version": None},
    ]


def test_build_items_falls_back_to_resource_id_when_no_title():
    plan = _plan(_action("4", action="download", reason="outdated", remote_version=1240))
    items = update_status.build_items_from_plan(plan)
    assert items == [{"resource_id": "4", "name": "4", "available_version_id": 1240, "version": None}]


def test_install_context_changed_is_not_an_update():
    """Re-downloads for path/settings changes aren't user-facing 'updates'."""
    plan = _plan(
        _action("4", action="download", reason="install_context_changed", title="KissAssist", remote_version=1240),
    )
    assert update_status.build_items_from_plan(plan) == []


def _result_item(resource_id, outcome, *, reason="outdated", target_key=None):
    from redfetch.sync_types import ExecutionResultItem

    return ExecutionResultItem(
        target_key=target_key or f"/{resource_id}/",
        resource_id=resource_id,
        outcome=outcome,
        reason=reason,
    )


def _exec_result(*items):
    from redfetch.sync_types import ExecutionResult

    return ExecutionResult(items={i.target_key: i for i in items})


def test_split_items_skipped_current_is_neither_installed_nor_remaining():
    plan = _plan(
        _action("4", action="download", reason="outdated", title="KissAssist", remote_version=1240),
    )
    result = _exec_result(_result_item("4", "skipped"))

    installed, remaining = update_status.split_items_by_outcome(plan, result)

    assert installed == []
    assert remaining == []


def test_split_items_missing_result_counts_as_remaining():
    """A planned download with no execution record (crash/cancel) is still outdated."""
    plan = _plan(
        _action("4", action="download", reason="outdated", title="KissAssist", remote_version=1240),
    )

    installed, remaining = update_status.split_items_by_outcome(plan, _exec_result())

    assert installed == []
    assert remaining == [{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240, "version": None}]


def test_split_items_keeps_blocked_exclusion():
    """The blocked-items exclusion is load-bearing: a lapsed subscriber must not
    spawn-loop on resources that can never install."""
    plan = _plan(
        _action("12", action="block", reason="needs_license", title="Lapsed"),
        _action("9", action="download", reason="not_installed", title="Fresh Install"),
    )
    result = _exec_result(
        _result_item("12", "blocked", reason="needs_license"),
        _result_item("9", "downloaded", reason="not_installed"),
    )

    installed, remaining = update_status.split_items_by_outcome(plan, result)

    assert installed == []
    assert remaining == []


def test_split_items_resource_with_any_failed_target_stays_remaining():
    """Same resource at two install targets: one failure keeps it out of installed."""
    plan = _plan(
        _action("4", action="download", reason="outdated", title="KissAssist", remote_version=1240),
        _action(
            "4", action="download", reason="outdated", title="KissAssist", remote_version=1240,
            target_kind="dependency", parent_id="7", root_resource_id="7",
        ),
    )
    result = _exec_result(
        _result_item("4", "downloaded"),
        _result_item("4", "error", target_key="/7/4/"),
    )

    installed, remaining = update_status.split_items_by_outcome(plan, result)

    assert installed == []
    assert remaining == [{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240, "version": None}]


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


def test_non_ok_states_force_empty_updates(status_dir):
    update_status.write_update_status(
        env="TEST",
        auth_state="needs_login",
        items=[{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}],
    )
    on_disk = _read_status(status_dir)
    assert on_disk["auth_state"] == "needs_login"
    assert on_disk["updates"]["items"] == []


def test_additive_fields_omitted_when_unset(status_dir):
    """check's writes never carry installed/pending_restart; stale ones age out."""
    update_status.write_update_status(env="LIVE", auth_state="ok", items=[])
    on_disk = _read_status(status_dir)
    assert "auto_update" not in on_disk
    assert "installed" not in on_disk
    assert "pending_restart" not in on_disk


def test_additive_fields_round_trip(status_dir):
    installed = [{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}]
    update_status.write_update_status(
        env="LIVE",
        auth_state="ok",
        items=[],
        auto_update=True,
        installed=installed,
        pending_restart=True,
    )
    on_disk = _read_status(status_dir)
    assert on_disk["auto_update"] is True
    assert on_disk["installed"] == installed
    assert on_disk["pending_restart"] is True
    assert on_disk["schema_version"] == update_status.SCHEMA_VERSION  # additive, no bump


def test_unicode_titles_survive_round_trip(status_dir):
    items = [{"resource_id": "1", "name": "Café Münster 日本語", "available_version_id": 1}]
    update_status.write_update_status(env="LIVE", auth_state="ok", items=items)
    assert _read_status(status_dir)["updates"]["items"][0]["name"] == "Café Münster 日本語"
