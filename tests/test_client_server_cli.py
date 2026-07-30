"""Tests for switching client environments and emu server profiles."""
from types import SimpleNamespace

import pytest
import typer

from redfetch import config, main, servers
from redfetch.main import Env


@pytest.fixture
def cli_env(monkeypatch):
    """Provide fake settings and an ordered call log."""
    fake = SimpleNamespace(ENV="LIVE")
    fake.from_env = lambda env: SimpleNamespace(as_dict=lambda: {"env": env})
    calls = []

    def fake_switch_environment(new_env):
        calls.append(("client", new_env))
        fake.ENV = new_env

    monkeypatch.setattr(config, "settings", fake)
    monkeypatch.setattr(config, "initialize_config", lambda: fake)
    monkeypatch.setattr(config, "switch_environment", fake_switch_environment)
    monkeypatch.setattr(servers, "switch_server", lambda slug: calls.append(("server", slug)))
    monkeypatch.setattr(
        servers, "add_server",
        lambda slug, **kwargs: calls.append(("add", slug, kwargs.get("eqpath"))),
    )
    monkeypatch.setattr(servers, "list_servers", lambda env="EMU": {})
    monkeypatch.setattr(servers, "is_server_configured", lambda slug, env="EMU": False)
    return fake, calls


def _set_known_server(monkeypatch, slug="thegrind", label="The Grind", configured=True):
    monkeypatch.setattr(servers, "list_servers", lambda env="EMU": {slug: {"label": label}})
    monkeypatch.setattr(servers, "is_server_configured", lambda s, env="EMU": configured)


# Client environment tokens

def test_token_switches_client_case_insensitively(cli_env):
    fake, calls = cli_env
    main.server_command(env="emu")
    assert calls == [("client", "EMU")]
    assert fake.ENV == "EMU"


def test_legacy_switch_env_enum_still_accepted(cli_env):
    """The legacy callback forwards an Env instance to the retyped command."""
    _, calls = cli_env
    main.server_command(env=Env.TEST)
    assert calls == [("client", "TEST")]


# Emulator server slugs

def test_slug_dispatches_to_switch_server(cli_env, monkeypatch):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(monkeypatch)
    main.server_command(env="thegrind")
    assert calls == [("server", "thegrind")]


def test_slug_from_live_switches_client_first(cli_env, monkeypatch):
    fake, calls = cli_env
    _set_known_server(monkeypatch)
    main.server_command(env="thegrind")
    assert calls == [("client", "EMU"), ("server", "thegrind")]
    assert fake.ENV == "EMU"


def test_unconfigured_known_slug_prompts_then_configures(cli_env, monkeypatch):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(
        monkeypatch, slug="lazarus", label="Project Lazarus", configured=False
    )
    monkeypatch.setattr(main.Prompt, "ask", lambda *a, **k: "D:/EQ-Laz")
    main.server_command(env="lazarus")
    assert calls == [("add", "lazarus", "D:/EQ-Laz"), ("server", "lazarus")]


def test_blank_folder_at_prompt_rejected(cli_env, monkeypatch):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(monkeypatch, slug="lazarus", configured=False)
    monkeypatch.setattr(main.Prompt, "ask", lambda *a, **k: "   ")
    monkeypatch.setattr(
        servers, "add_server",
        lambda slug, **kwargs: (_ for _ in ()).throw(ValueError("needs an EverQuest folder")),
    )
    with pytest.raises(typer.BadParameter, match="folder"):
        main.server_command(env="lazarus")
    assert calls == []


# Rejections

@pytest.mark.parametrize("value", ["The Grind", "grind!", "grind/zek", "grind.zek"])
def test_invalid_server_names_rejected(cli_env, value):
    _, calls = cli_env
    with pytest.raises(typer.BadParameter):
        main.server_command(env=value)
    assert calls == []


def test_unknown_slug_lists_tokens_and_servers(cli_env, monkeypatch):
    _, calls = cli_env
    _set_known_server(monkeypatch, slug="lazarus")
    with pytest.raises(typer.BadParameter) as exc_info:
        main.server_command(env="nope")
    message = str(exc_info.value)
    assert "LIVE" in message and "EMU" in message and "lazarus" in message
    assert calls == []


def test_blocked_switch_exits_nonzero(cli_env, monkeypatch):
    """Writer guard failures become a normal CLI exit."""
    fake, _ = cli_env
    fake.ENV = "EMU"
    _set_known_server(monkeypatch)
    monkeypatch.setattr(
        servers, "switch_server",
        lambda slug: (_ for _ in ()).throw(servers.ServerSwitchError("no EverQuest folder yet")),
    )
    with pytest.raises(typer.Exit) as exc_info:
        main.server_command(env="thegrind")
    assert exc_info.value.exit_code == 1
