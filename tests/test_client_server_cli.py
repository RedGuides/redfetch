"""Tests for switching client environments and emu servers."""
import inspect
from types import SimpleNamespace

import pytest
import typer

from redfetch import config, main, servers
from redfetch.main import Env


@pytest.fixture
def cli_env(monkeypatch):
    """Provide fake settings and an ordered call log."""
    fake = SimpleNamespace(ENV="LIVE", active_server=None)
    fake.from_env = lambda env: SimpleNamespace(
        as_dict=lambda: {"env": env},
        # Env-scoped like the real thing: only a multi-server env can hold one.
        get=lambda key, default=None: (
            fake.active_server
            if key == "ACTIVE_SERVER" and env in config.MULTI_SERVER_ENVS
            else default
        ),
    )
    calls = []

    def fake_switch_environment(new_env):
        calls.append(("client", new_env))
        fake.ENV = new_env

    def fake_switch_to_generic(env):
        calls.append(("bare", env))
        fake.active_server = None
        return []

    def fake_switch_server(slug):
        calls.append(("server", slug))
        return []  # the real one always returns a notices list

    monkeypatch.setattr(config, "settings", fake)
    monkeypatch.setattr(config, "initialize_config", lambda: fake)
    monkeypatch.setattr(config, "switch_environment", fake_switch_environment)
    monkeypatch.setattr(servers, "switch_server", fake_switch_server)
    monkeypatch.setattr(servers, "switch_to_generic", fake_switch_to_generic)
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


# The client command and the deprecated client tokens

def test_client_command_switches_client(cli_env):
    fake, calls = cli_env
    main.client_command(env=Env.EMU)
    assert calls == [("client", "EMU")]
    assert fake.ENV == "EMU"


def test_client_command_leaves_active_server(cli_env):
    """The un-amended C5 contract: a client switch resumes the active server."""
    fake, calls = cli_env
    fake.ENV = "EMU"
    fake.active_server = "lazarus"

    main.client_command(env=Env.EMU)

    assert calls == [("client", "EMU")]
    assert fake.active_server == "lazarus"


def test_token_switches_client_case_insensitively(cli_env):
    fake, calls = cli_env
    main.server_command(server="emu")
    assert calls == [("client", "EMU")]
    assert fake.ENV == "EMU"


def test_token_via_server_command_leaves_active_server(cli_env):
    """Same un-amended contract through the deprecated 'server <token>' spelling."""
    fake, calls = cli_env
    fake.ENV = "EMU"
    fake.active_server = "lazarus"

    main.server_command(server="emu")

    assert calls == [("client", "EMU")]
    assert fake.active_server == "lazarus"


def test_token_via_server_command_does_not_nag(cli_env, capsys):
    """Tokens are cheap to handle, so no deprecation note."""
    main.server_command(server="emu")
    assert "redfetch client" not in capsys.readouterr().out


def test_legacy_switch_env_enum_still_accepted(cli_env):
    """An Env instance still routes as a client token (str-subclass robustness)."""
    _, calls = cli_env
    main.server_command(server=Env.TEST)
    assert calls == [("client", "TEST")]


def test_env_enum_pins_envs():
    """The CLI's client-token axis stays in lockstep with config.ENVS."""
    assert set(Env) == set(config.ENVS)


# The bare-setup token

def test_server_none_switches_to_bare_setup(cli_env):
    fake, calls = cli_env
    fake.ENV = "EMU"
    fake.active_server = "lazarus"

    main.server_command(server="none")

    assert calls == [("bare", "EMU")]
    assert fake.active_server is None


def test_server_none_noop_when_already_bare(cli_env):
    fake, calls = cli_env
    fake.ENV = "EMU"
    main.server_command(server="none")
    assert calls == []


def test_server_none_rejected_on_single_server_client(cli_env):
    _, calls = cli_env
    with pytest.raises(typer.BadParameter, match="emu client"):
        main.server_command(server="none")
    assert calls == []


# Emulator server slugs

def test_slug_dispatches_to_switch_server(cli_env, monkeypatch):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(monkeypatch)
    main.server_command(server="thegrind")
    assert calls == [("server", "thegrind")]


def test_slug_from_live_switches_client_first(cli_env, monkeypatch):
    fake, calls = cli_env
    _set_known_server(monkeypatch)
    main.server_command(server="thegrind")
    assert calls == [("client", "EMU"), ("server", "thegrind")]
    assert fake.ENV == "EMU"


def test_unconfigured_known_slug_prompts_then_configures(cli_env, monkeypatch):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(
        monkeypatch, slug="lazarus", label="Project Lazarus", configured=False
    )
    monkeypatch.setattr(main.Prompt, "ask", lambda *a, **k: "D:/EQ-Laz")
    main.server_command(server="lazarus")
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
        main.server_command(server="lazarus")
    assert calls == []


# Rejections

@pytest.mark.parametrize("value", ["The Grind", "grind!", "grind/zek", "grind.zek"])
def test_invalid_server_names_rejected(cli_env, value):
    _, calls = cli_env
    with pytest.raises(typer.BadParameter):
        main.server_command(server=value)
    assert calls == []


def test_unknown_slug_lists_servers_and_points_at_client(cli_env, monkeypatch):
    _, calls = cli_env
    _set_known_server(monkeypatch, slug="lazarus")
    with pytest.raises(typer.BadParameter) as exc_info:
        main.server_command(server="nope")
    message = str(exc_info.value)
    assert "none" in message and "lazarus" in message
    assert "redfetch client" in message
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
        main.server_command(server="thegrind")
    assert exc_info.value.exit_code == 1


# Per-run override flags

def test_server_flags_carry_client_alias():
    """--client is accepted everywhere --server is, so callers can migrate."""
    flagged = []
    for info in main.app.registered_commands:
        for param in inspect.signature(info.callback).parameters.values():
            decls = getattr(param.default, "param_decls", None) or ()
            if "--server" in decls:
                flagged.append((info.callback.__name__, decls))
    assert flagged
    for name, decls in flagged:
        assert "--client" in decls, f"{name}'s --server lacks the --client alias"
