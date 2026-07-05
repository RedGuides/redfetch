"""Startup summaries count every planned download, not only outdated resources."""

from redfetch.sync_types import ExecutionPlan, PlannedAction


def _action(resource_id, *, action, reason):
    return PlannedAction(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        root_resource_id=resource_id,
        target_kind="root",
        action=action,
        reason=reason,
    )


def test_counts_every_download_reason(monkeypatch):
    from redfetch import config

    if config.settings is None:
        monkeypatch.setattr(config, "settings", type("S", (), {"ENV": "LIVE"}))
    from redfetch.terminal_ui import _startup_update_summary

    plan = ExecutionPlan(actions={a.target_key: a for a in (
        _action("1", action="download", reason="outdated"),
        _action("2", action="download", reason="not_installed"),
        _action("3", action="download", reason="install_context_changed"),
        _action("4", action="skip", reason="already_current"),
        _action("5", action="untrack", reason="not_desired"),
    )})

    count, message = _startup_update_summary(plan)
    assert count == 3  # Matches the executor's download count.
    assert message.startswith("3 resources to update")
