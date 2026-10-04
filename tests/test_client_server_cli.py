"""Tests for switching client environments and emu servers."""
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner
from conftest import flat_output

from redfetch import config, main, servers
from redfetch.main import Env

runner = CliRunner()


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


@pytest.mark.parametrize("typed, token", [
    ("EMU", "EMU"), ("emu", "EMU"), ("RoF2", "EMU"), ("rof2", "EMU"), (" rof2 ", "EMU"),
    ("live", "LIVE"), ("Test", "TEST"),
])
def test_env_token_accepts_token_or_label_any_case(typed, token):
    assert config.env_token(typed) == token


@pytest.mark.parametrize("typed", ["titanium", "", "lazarus", "emu (rof2)"])
def test_env_token_rejects_non_clients(typed):
    assert config.env_token(typed) is None


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


def _eq_folder(tmp_path):
    """A real EverQuest folder — the prompt path now validates eqgame.exe."""
    folder = tmp_path / "EverQuest"
    folder.mkdir()
    (folder / "eqgame.exe").write_bytes(b"MZ")
    return folder


def test_unconfigured_known_slug_prompts_then_configures(cli_env, monkeypatch, tmp_path):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(
        monkeypatch, slug="lazarus", label="Project Lazarus", configured=False
    )
    folder = _eq_folder(tmp_path)
    monkeypatch.setattr(main.Prompt, "ask", lambda *a, **k: str(folder))
    main.server_command(server="lazarus")
    assert calls == [("add", "lazarus", str(folder)), ("server", "lazarus")]


def test_folder_without_eqgame_at_prompt_rejected(cli_env, monkeypatch, tmp_path):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(monkeypatch, slug="lazarus", configured=False)
    not_eq = tmp_path / "NotEverQuest"
    not_eq.mkdir()
    monkeypatch.setattr(main.Prompt, "ask", lambda *a, **k: str(not_eq))
    with pytest.raises(typer.BadParameter, match="eqgame.exe"):
        main.server_command(server="lazarus")
    # A refusal adds nothing and never switches.
    assert calls == []


def test_blank_folder_at_prompt_rejected(cli_env, monkeypatch):
    fake, calls = cli_env
    fake.ENV = "EMU"
    _set_known_server(monkeypatch, slug="lazarus", configured=False)
    monkeypatch.setattr(main.Prompt, "ask", lambda *a, **k: "   ")
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


# Typing a client by label (the CLI parser hook; config.env_token does the matching)

def _cli_calls(monkeypatch):
    """CliRunner harness: record what the parsed client resolved to instead of switching."""
    calls = []
    monkeypatch.setattr(config, "initialize_config", lambda: None)
    monkeypatch.setattr(main, "_switch_client", lambda token: calls.append(("client", token)))
    monkeypatch.setattr(main, "initialize_db_only", lambda server=None: (calls.append(("db", server)), ("db", "path"))[1])
    monkeypatch.setattr(main.asyncio, "run", lambda coro: coro.close())
    return calls


@pytest.mark.parametrize("typed", ["emu", "EMU", "rof2", "RoF2"])
def test_client_command_accepts_token_or_label(monkeypatch, typed):
    calls = _cli_calls(monkeypatch)
    result = runner.invoke(main.app, ["client", typed])
    assert result.exit_code == 0, result.output
    assert calls == [("client", "EMU")]


def test_server_command_accepts_client_label(monkeypatch):
    """`redfetch server rof2` switches the client the same way `server emu` does."""
    calls = _cli_calls(monkeypatch)
    result = runner.invoke(main.app, ["server", "rof2"])
    assert result.exit_code == 0, result.output
    assert calls == [("client", "EMU")]


@pytest.mark.parametrize("typed", ["rof2", "EMU"])
def test_per_run_override_accepts_token_or_label(monkeypatch, typed):
    calls = _cli_calls(monkeypatch)
    result = runner.invoke(main.app, ["update", "-s", typed])
    assert result.exit_code == 0, result.output
    assert calls == [("db", Env.EMU)]


def test_legacy_switch_env_accepts_label(monkeypatch):
    calls = _cli_calls(monkeypatch)
    result = runner.invoke(main.app, ["--switch-env", "rof2"])
    assert result.exit_code == 0, result.output
    assert calls == [("client", "EMU")]


@pytest.mark.parametrize("argv", [["client", "titanium"], ["update", "-s", "titanium"]])
def test_unknown_client_is_a_usage_error_listing_choices(monkeypatch, argv):
    calls = _cli_calls(monkeypatch)
    result = runner.invoke(main.app, argv)
    assert result.exit_code == 2
    out = flat_output(result)
    assert "'titanium' is not a client" in out
    assert "LIVE, TEST, or EMU (RoF2)" in out  # plain: typer's error box doesn't render markup
    assert calls == []


@pytest.mark.parametrize("command", ["client", "update"])
def test_help_lists_clients_with_labels(command):
    result = runner.invoke(main.app, [command, "--help"])
    assert result.exit_code == 0
    assert "EMU (RoF2)" in flat_output(result)


# Per-run override flags

def test_server_flags_carry_client_alias():
    """--client is accepted everywhere --server is, so callers can migrate."""
    flagged = [
        (name, param.opts)
        for name, command in typer.main.get_command(main.app).commands.items()
        for param in command.params
        if "--server" in param.opts
    ]
    assert flagged
    for name, opts in flagged:
        assert "--client" in opts, f"{name}'s --server lacks the --client alias"
