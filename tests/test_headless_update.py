"""`update --headless` (the MQ-spawned form): check-style init, no prompts, no
browser, no dialogs; binary exit contract with all nuance in update_status.json."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import typer

from redfetch import config, main, update_status
from redfetch.main import Env
from redfetch.sync_types import (
    ExecutionPlan,
    ExecutionResult,
    ExecutionResultItem,
    PlannedAction,
    SyncOutcome,
)


def _action(resource_id, *, reason="outdated", action="download", title=None, remote_version=None):
    return PlannedAction(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        root_resource_id=resource_id,
        target_kind="root",
        action=action,
        reason=reason,
        title=title,
        remote_version=remote_version,
    )


def _result_item(resource_id, outcome, *, reason="outdated"):
    return ExecutionResultItem(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        outcome=outcome,
        reason=reason,
    )


def _outcome(actions, result_items, *, success=True, vvmq_updated=False):
    return SyncOutcome(
        success=success,
        vvmq_updated=vvmq_updated,
        execution_plan=ExecutionPlan(actions={a.target_key: a for a in actions}),
        execution_result=ExecutionResult(items={i.target_key: i for i in result_items}),
    )


def _read_status(tmp_path):
    return json.loads(
        (tmp_path / update_status.UPDATE_STATUS_FILENAME).read_text(encoding="utf-8")
    )


def _run_headless(server=None, force=False):
    with pytest.raises(typer.Exit) as exc_info:
        main.update_command(force=force, server=server, headless=True)
    return exc_info.value.exit_code


@pytest.fixture
def headless_env(monkeypatch, tmp_path):
    """Configured, credentialed, AUTO_UPDATE on; prompts and dialogs booby-trapped."""
    settings = SimpleNamespace(ENV="LIVE")
    settings.setenv = lambda new_env: setattr(settings, "ENV", new_env)
    settings.validators = SimpleNamespace(validate=lambda: None)
    # Dict-backed so tests can persist per-env values and exercise the real accessors.
    env_values: dict = {}
    settings.from_env = lambda _env: SimpleNamespace(
        get=lambda key, default=None: env_values.get(key, default)
    )

    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "initialize_config", lambda: settings)
    monkeypatch.setattr("redfetch.config_firstrun.is_configured", lambda *a, **k: True)
    monkeypatch.setattr(main.auth, "initialize_keyring", lambda: None)
    monkeypatch.setattr(main, "_has_auth_credentials", lambda: True)
    monkeypatch.setattr(main.utils, "get_vvmq_path", lambda: r"D:\MQ\VanillaMQ_LIVE")
    monkeypatch.setattr(main.utils, "sweep_stale_update_debris", lambda: None)
    monkeypatch.setattr(main.store, "initialize_db", lambda db_name: None)
    monkeypatch.setattr(main.store, "get_db_path", lambda db_name: ":memory:")

    # The dialog/prompt ban is load-bearing: no headless path may reach these.
    # pytest.fail raises a BaseException, so the trap pierces the headless
    # catch-all instead of being converted into an expected exit 1.
    def _prompt_trap(*a, **k):
        pytest.fail("headless must never prompt")

    monkeypatch.setattr("rich.prompt.Prompt.ask", _prompt_trap)
    monkeypatch.setattr("rich.prompt.Confirm.ask", _prompt_trap)
    monkeypatch.setattr(
        main, "exit_with_fatal_error",
        lambda exc: pytest.fail(f"fatal-error dialog reached from headless: {exc!r}"),
    )
    monkeypatch.setattr(
        main.meta, "check_for_update",
        lambda: pytest.fail("headless must not run the interactive update interview"),
    )

    async def _no_offer(*a, **k):
        pytest.fail("headless must never run post-update offers")

    monkeypatch.setattr(main.post_update, "offer", _no_offer)

    spawned = []
    monkeypatch.setattr(main.meta, "spawn_silent_self_update", lambda: spawned.append(True))

    async def _headers(**k):
        return {"h": "1"}

    monkeypatch.setattr(main.auth, "get_api_headers", _headers)

    return SimpleNamespace(
        settings=settings, tmp_path=tmp_path, monkeypatch=monkeypatch, spawned=spawned,
        env_values=env_values,
    )


def _set_run_sync(env, outcome):
    async def _run_sync(*a, **k):
        return outcome

    env.monkeypatch.setattr(main.sync, "run_sync", _run_sync)


def test_not_configured_writes_verdict_never_wizard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("redfetch.config_firstrun.is_configured", lambda *a, **k: False)
    monkeypatch.setattr(
        config, "initialize_config",
        lambda: pytest.fail("not-configured headless must not initialize config (first-run wizard)"),
    )

    assert _run_headless(server=Env.EMU) == 0
    on_disk = _read_status(tmp_path)
    assert on_disk["auth_state"] == "not_configured"
    assert on_disk["env"] == "EMU"
    assert "auto_update" not in on_disk  # pre-init write can't read settings


def test_no_credentials_writes_needs_login_no_browser(headless_env):
    headless_env.monkeypatch.setattr(main, "_has_auth_credentials", lambda: False)
    headless_env.monkeypatch.setattr(
        main.auth, "authorize",
        lambda: pytest.fail("headless must never reach authorize() (browser)"),
    )

    assert _run_headless() == 0
    on_disk = _read_status(headless_env.tmp_path)
    assert on_disk["auth_state"] == "needs_login"
    assert on_disk["auto_update"] is True
    assert on_disk["updates"]["items"] == []
    assert headless_env.spawned == []  # no self-update without a clean run


def test_midrun_refresh_failure_writes_needs_login(headless_env):
    async def _expired(**k):
        raise RuntimeError("token expired")

    headless_env.monkeypatch.setattr(main.auth, "get_api_headers", _expired)

    assert _run_headless() == 0
    on_disk = _read_status(headless_env.tmp_path)
    assert on_disk["auth_state"] == "needs_login"
    assert on_disk["managed_path"] == r"D:\MQ\VanillaMQ_LIVE"


def test_completion_write_derives_items_from_execution(headless_env):
    outcome = _outcome(
        [
            _action("4", title="KissAssist", remote_version=1240),
            _action("3040", title="RGMercs", remote_version=991),
            # A fresh install is not an "update": excluded from both lists.
            _action("9", reason="not_installed", title="New Thing", remote_version=5),
        ],
        [
            _result_item("4", "downloaded"),
            _result_item("3040", "error"),
            _result_item("9", "downloaded", reason="not_installed"),
        ],
        success=False,  # the partial failure must NOT hide per-resource failures
        vvmq_updated=True,
    )
    _set_run_sync(headless_env, outcome)

    assert _run_headless() == 0  # ran and wrote status: nuance lives in the file
    on_disk = _read_status(headless_env.tmp_path)
    assert on_disk["auth_state"] == "ok"
    assert on_disk["env"] == "LIVE"
    assert on_disk["managed_path"] == r"D:\MQ\VanillaMQ_LIVE"
    assert on_disk["auto_update"] is True
    assert on_disk["pending_restart"] is True
    # Remaining = the failed item only: not [] (hides failures), not the full
    # pre-execution plan (re-lists what just installed and feeds the spawn loop).
    assert on_disk["updates"]["items"] == [
        {"resource_id": "3040", "name": "RGMercs", "available_version_id": 991}
    ]
    assert on_disk["installed"] == [
        {"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}
    ]


def test_clean_run_spawns_self_update_after_status_write(headless_env):
    outcome = _outcome(
        [_action("4", title="KissAssist", remote_version=1240)],
        [_result_item("4", "downloaded")],
    )
    _set_run_sync(headless_env, outcome)

    write_seen_by_spawn = []

    def _spawn():
        # The ordering is the safety argument: the swap starts only after this
        # run has nothing left to do and the status file is already fresh.
        write_seen_by_spawn.append(_read_status(headless_env.tmp_path)["auth_state"])

    headless_env.monkeypatch.setattr(main.meta, "spawn_silent_self_update", _spawn)

    assert _run_headless() == 0
    assert write_seen_by_spawn == ["ok"]


def test_auto_update_off_exits_1_before_any_network(headless_env):
    # Persisted opt-out read through the REAL accessor: the consent gate end-to-end.
    headless_env.env_values["AUTO_UPDATE"] = False

    async def _no_net(*a, **k):
        pytest.fail("opted-out headless must not touch the network")

    headless_env.monkeypatch.setattr(main.auth, "get_api_headers", _no_net)
    headless_env.monkeypatch.setattr(main.sync, "run_sync", _no_net)

    assert _run_headless() == 1
    assert not (headless_env.tmp_path / update_status.UPDATE_STATUS_FILENAME).exists()


def test_auto_update_accessor_fails_closed(monkeypatch):
    # Uninitialized settings -> never spawn-worthy, never a crash.
    monkeypatch.setattr(config, "settings", None)
    assert main.utils.is_auto_update_enabled() is False


def test_server_composes_with_headless(headless_env):
    """--server must select the env in memory for the run (db + status) and never persist it."""
    for fn in ("switch_environment", "write_env_to_file"):
        headless_env.monkeypatch.setattr(
            config, fn,
            lambda *a, **k: pytest.fail("--server must not persist the environment"),
        )
    db_names = []
    headless_env.monkeypatch.setattr(main.store, "initialize_db", db_names.append)
    _set_run_sync(headless_env, _outcome([], []))

    assert _run_headless(server=Env.TEST) == 0
    assert db_names == ["TEST_resources.db"]
    assert _read_status(headless_env.tmp_path)["env"] == "TEST"


def test_lock_busy_exits_1_without_status_write(headless_env):
    _set_run_sync(headless_env, SyncOutcome(success=False, status="busy"))

    assert _run_headless() == 1
    assert not (headless_env.tmp_path / update_status.UPDATE_STATUS_FILENAME).exists()
    assert headless_env.spawned == []


def test_midrun_exception_exits_1_no_dialog(headless_env):
    async def _boom(*a, **k):
        raise ValueError("disk exploded")

    headless_env.monkeypatch.setattr(main.sync, "run_sync", _boom)

    # exit 1, and the fixture's exit_with_fatal_error trap proves no MessageBox path.
    assert _run_headless() == 1


def test_force_composes_with_headless(headless_env):
    reset = []

    class _Cursor:
        pass

    class _Conn:
        def cursor(self):
            return _Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    headless_env.monkeypatch.setattr(main.store, "get_db_connection", lambda db_name: _Conn())
    headless_env.monkeypatch.setattr(
        main.store, "reset_download_dates", lambda cursor: reset.append(True)
    )
    _set_run_sync(headless_env, _outcome([], []))

    assert _run_headless(force=True) == 0
    assert reset == [True]
