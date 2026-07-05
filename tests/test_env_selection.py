"""Tests for environment selection via --server."""
import json
from types import SimpleNamespace

import pytest
import typer

from redfetch.main import Env, _apply_server_override
from redfetch import config, main, update_status


@pytest.fixture
def fake_config(monkeypatch):
    """Patch config.settings with a fake, capture ephemeral selections, fail on any persist."""
    fake = SimpleNamespace(ENV="LIVE")
    monkeypatch.setattr(config, "settings", fake)
    selected = []

    def fake_select(new_env):
        fake.ENV = new_env
        selected.append(new_env)

    monkeypatch.setattr(config, "select_environment_in_memory", fake_select)
    monkeypatch.setattr(
        config, "switch_environment",
        lambda *a, **k: pytest.fail("--server must not persist the environment"),
    )
    return fake, selected


def test_server_flag_selects_environment_in_memory(fake_config):
    """--server EMU while on LIVE selects EMU for this run only, without persisting."""
    settings, selected = fake_config
    _apply_server_override(server=Env.EMU)
    assert selected == ["EMU"]
    assert settings.ENV == "EMU"


def test_server_flag_noop_when_already_on_env(fake_config):
    """--server LIVE while already on LIVE must be a no-op."""
    settings, selected = fake_config
    _apply_server_override(server=Env.LIVE)
    assert selected == []
    assert settings.ENV == "LIVE"


def test_server_flag_noop_when_omitted(fake_config):
    """No --server at all must leave the environment untouched."""
    settings, selected = fake_config
    _apply_server_override(server=None)
    assert selected == []
    assert settings.ENV == "LIVE"


def _fake_settings(env="LIVE"):
    """A minimal stand-in for config.settings that supports in-memory env selection."""
    calls = {"setenv": [], "validated": 0}
    settings = SimpleNamespace(ENV=env)
    settings.setenv = lambda new_env: (calls["setenv"].append(new_env), setattr(settings, "ENV", new_env))
    settings.validators = SimpleNamespace(
        validate=lambda: calls.__setitem__("validated", calls["validated"] + 1)
    )
    return settings, calls


def test_select_environment_in_memory_does_not_persist(monkeypatch):
    """Ephemeral selection sets the env for this process but must never touch the .env file."""
    settings, calls = _fake_settings("LIVE")
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(
        config, "write_env_to_file",
        lambda *a, **k: pytest.fail("select_environment_in_memory must not persist to .env"),
    )

    config.select_environment_in_memory("TEST")

    assert settings.ENV == "TEST"
    assert calls["setenv"] == ["TEST"]
    assert calls["validated"] == 1


def test_check_command_uses_ephemeral_env(monkeypatch, tmp_path):
    """`check --server TEST` must report env=TEST in the file without persisting the switch."""
    settings, _ = _fake_settings("LIVE")
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "initialize_config", lambda: settings)
    monkeypatch.setattr("redfetch.config_firstrun.is_configured", lambda *a, **k: True)
    monkeypatch.setattr(main, "_has_auth_credentials", lambda: True)
    monkeypatch.setattr(main.auth, "initialize_keyring", lambda: None)
    monkeypatch.setattr(main.store, "initialize_db", lambda db_name: None)
    monkeypatch.setattr(main.store, "get_db_path", lambda db_name: ":memory:")

    def _must_not_persist(*a, **k):
        raise AssertionError("check must not persist the environment")

    monkeypatch.setattr(config, "switch_environment", _must_not_persist)
    monkeypatch.setattr(config, "write_env_to_file", _must_not_persist)
    monkeypatch.setattr(main.utils, "get_vvmq_path", lambda: r"D:\MQ\VanillaMQ_TEST")

    async def fake_check(db_path):
        return "ok", [{"resource_id": "4", "name": "KissAssist", "available_version_id": 1240}]

    monkeypatch.setattr(main, "_check_command_async", fake_check)

    with pytest.raises(typer.Exit) as exc_info:
        main.check_command(server=Env.TEST)
    assert exc_info.value.exit_code == 0

    on_disk = json.loads(
        (tmp_path / update_status.UPDATE_STATUS_FILENAME).read_text(encoding="utf-8")
    )
    assert on_disk["env"] == "TEST"
    assert on_disk["auth_state"] == "ok"
    assert len(on_disk["updates"]["items"]) == 1
    assert on_disk["managed_path"] == r"D:\MQ\VanillaMQ_TEST"


def test_check_command_not_configured_writes_verdict(monkeypatch, tmp_path):
    """Not configured at all: still record a verdict (exit 0) honoring the requested env, no updates."""
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("redfetch.config_firstrun.is_configured", lambda *a, **k: False)
    monkeypatch.setattr(
        config, "initialize_config",
        lambda: pytest.fail("not-configured path must not initialize config"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        main.check_command(server=Env.EMU)
    assert exc_info.value.exit_code == 0

    on_disk = json.loads(
        (tmp_path / update_status.UPDATE_STATUS_FILENAME).read_text(encoding="utf-8")
    )
    assert on_disk["auth_state"] == "not_configured"
    assert on_disk["env"] == "EMU"
    assert on_disk["updates"]["items"] == []
    # Not configured: redfetch can't resolve a managed tree, so MQ won't gate.
    assert on_disk["managed_path"] is None


def test_check_command_needs_login_writes_verdict(monkeypatch, tmp_path):
    """Configured but missing credentials: record needs_login (exit 0) without running a sync."""
    settings, _ = _fake_settings("LIVE")
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "initialize_config", lambda: settings)
    monkeypatch.setattr("redfetch.config_firstrun.is_configured", lambda *a, **k: True)
    monkeypatch.setattr(main.auth, "initialize_keyring", lambda: None)
    monkeypatch.setattr(main, "_has_auth_credentials", lambda: False)
    monkeypatch.setattr(main.utils, "get_vvmq_path", lambda: r"D:\MQ\VanillaMQ_LIVE")

    def _must_not_run(*a, **k):
        raise AssertionError("needs_login path must not start a sync")

    monkeypatch.setattr(main, "_check_command_async", _must_not_run)

    with pytest.raises(typer.Exit) as exc_info:
        main.check_command(server=None)
    assert exc_info.value.exit_code == 0

    on_disk = json.loads(
        (tmp_path / update_status.UPDATE_STATUS_FILENAME).read_text(encoding="utf-8")
    )
    assert on_disk["auth_state"] == "needs_login"
    assert on_disk["env"] == "LIVE"
    assert on_disk["updates"]["items"] == []
    assert on_disk["managed_path"] == r"D:\MQ\VanillaMQ_LIVE"
