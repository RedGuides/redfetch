"""Tests for the shared shortcuts registry (redfetch run / open)."""

import os

import pytest
from typer.testing import CliRunner

from redfetch import shortcuts, processes, config, main

windows_only = pytest.mark.skipif(os.name != "nt", reason="uses the Win32 profile API")

runner = CliRunner()


@pytest.fixture
def stub_init(monkeypatch):
    """Skip real config loading so CLI tests are hermetic (also runs on CI/linux)."""
    monkeypatch.setattr(config, "initialize_config", lambda: None)
    return monkeypatch


# --- registry integrity -----------------------------------------------------

def test_no_duplicate_names_within_each_namespace():
    run_names = [n for r in shortcuts.RUNNABLES for n in (r.key, *r.aliases)]
    open_names = [n for o in shortcuts.OPENABLES for n in (o.key, *o.aliases)]
    assert len(run_names) == len(set(run_names)), run_names
    assert len(open_names) == len(set(open_names)), open_names


def test_every_run_name_resolves_case_insensitively():
    for r in shortcuts.RUNNABLES:
        for name in (r.key, *r.aliases):
            assert shortcuts.find_runnable(name) is r
            assert shortcuts.find_runnable(name.upper()) is r
            assert shortcuts.find_runnable(f"  {name} ") is r


def test_every_open_name_resolves_case_insensitively():
    for o in shortcuts.OPENABLES:
        for name in (o.key, *o.aliases):
            assert shortcuts.find_openable(name) is o
            assert shortcuts.find_openable(name.upper()) is o


# --- registry contents (guards against accidental edits) --------------------

def test_known_static_attributes():
    assert shortcuts.find_runnable("mq").executable == "MacroQuest.exe"
    assert shortcuts.find_runnable("eqgame").args == ("patchme",)
    assert shortcuts.find_runnable("meshgenerator").executable == "MeshGenerator.exe"
    assert shortcuts.find_runnable("mesh").executable == "MeshGenerator.exe"  # alias preserved
    assert shortcuts.find_runnable("meshgen").prepare is shortcuts._seed_meshgen_ini
    assert shortcuts.find_openable("settings").filename == "settings.local.toml"
    assert shortcuts.find_openable("mq-config").css == "file"
    assert shortcuts.find_openable("downloads").filename is None  # a folder


# --- run() dispatch ---------------------------------------------------------

def test_run_passes_resolved_dir_and_merged_args(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args: calls.append((folder, exe, args)))
    r = shortcuts.Runnable("t", "L", "Foo.exe", lambda: "C:/x", args=("a",))
    shortcuts.run(r, extra=["b"])
    assert calls == [("C:/x", "Foo.exe", ["a", "b"])]


def test_run_invokes_prepare_hook_before_launch(monkeypatch):
    events = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args: events.append("run"))
    r = shortcuts.Runnable("t", "L", "Foo.exe", lambda: "C:/x",
                           prepare=lambda: events.append("prepare"))
    shortcuts.run(r)
    assert events == ["prepare", "run"]


# --- meshgen INI seeding (Windows-only: exercises the real Win32 profile API) ----

@windows_only
def test_seed_meshgen_ini_writes_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts.utils, "get_vvmq_path", lambda: str(tmp_path))
    monkeypatch.setattr(shortcuts, "_eq_dir", lambda: r"C:\Games\EverQuest")
    (tmp_path / "config").mkdir()

    shortcuts._seed_meshgen_ini()

    ini = (tmp_path / "config" / "MeshGenerator.ini").read_text()
    assert f"Output Path={tmp_path}" in ini
    assert r"EverQuest Path=C:\Games\EverQuest" in ini


@windows_only
def test_seed_meshgen_ini_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts.utils, "get_vvmq_path", lambda: str(tmp_path))
    monkeypatch.setattr(shortcuts, "_eq_dir", lambda: r"C:\NEW")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "MeshGenerator.ini").write_text(
        "[General]\nEverQuest Path=C:\\OLD\nZoneMaxExtents=1\n"
    )

    shortcuts._seed_meshgen_ini()

    ini = (cfg / "MeshGenerator.ini").read_text()
    assert "C:\\OLD" in ini and "C:\\NEW" not in ini   # existing EQ path untouched
    assert "ZoneMaxExtents=1" in ini                   # unrelated key untouched
    assert f"Output Path={tmp_path}" in ini            # still seeds the missing key


@windows_only
def test_seed_meshgen_ini_skips_when_eq_path_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts.utils, "get_vvmq_path", lambda: str(tmp_path))
    monkeypatch.setattr(shortcuts, "_eq_dir", lambda: None)
    (tmp_path / "config").mkdir()

    shortcuts._seed_meshgen_ini()

    ini = (tmp_path / "config" / "MeshGenerator.ini").read_text()
    assert f"Output Path={tmp_path}" in ini
    assert "EverQuest Path" not in ini


# --- open_target() dispatch -------------------------------------------------

def test_open_folder_dispatch(monkeypatch):
    opened = []
    monkeypatch.setattr(processes, "open_folder", lambda path: opened.append(path))
    o = shortcuts.Openable("t", "L", lambda: "C:/d")
    assert shortcuts.open_target(o) == ""
    assert opened == ["C:/d"]


def test_open_file_dispatch_returns_descriptor(monkeypatch):
    opened = []
    monkeypatch.setattr(processes, "open_file",
                        lambda folder, name: opened.append((folder, name)) or "with Notepad")
    o = shortcuts.Openable("t", "L", lambda: "C:/d", "f.ini")
    assert shortcuts.open_target(o) == "with Notepad"
    assert opened == [("C:/d", "f.ini")]


def test_open_runs_prepare_hook_before_opening(monkeypatch):
    events = []
    monkeypatch.setattr(processes, "open_file",
                        lambda folder, name: events.append("open") or "")
    o = shortcuts.Openable("t", "L", lambda: "C:/d", "f.ini",
                           prepare=lambda: events.append("prepare"))
    shortcuts.open_target(o)
    assert events == ["prepare", "open"]


def test_open_missing_path_raises(monkeypatch):
    o = shortcuts.Openable("t", "L", lambda: None)
    with pytest.raises(FileNotFoundError):
        shortcuts.open_target(o)


# --- availability -----------------------------------------------------------

def test_openable_available_folder(tmp_path):
    o = shortcuts.Openable("t", "L", lambda: str(tmp_path))
    assert shortcuts.openable_available(o) is True
    missing = shortcuts.Openable("t", "L", lambda: str(tmp_path / "nope"))
    assert shortcuts.openable_available(missing) is False
    unset = shortcuts.Openable("t", "L", lambda: None)
    assert shortcuts.openable_available(unset) is False


def test_openable_available_file(tmp_path):
    (tmp_path / "f.ini").write_text("")
    present = shortcuts.Openable("t", "L", lambda: str(tmp_path), "f.ini")
    assert shortcuts.openable_available(present) is True
    absent = shortcuts.Openable("t", "L", lambda: str(tmp_path), "missing.ini")
    assert shortcuts.openable_available(absent) is False


# --- CLI: `redfetch run` / `redfetch open` ----------------------------------

def test_cli_run_launches(stub_init):
    launched = []
    stub_init.setattr(shortcuts, "run", lambda r, extra=None: launched.append(r))
    result = runner.invoke(main.app, ["run", "vvmq"])
    assert result.exit_code == 0, result.output
    assert launched and launched[0].key == "vvmq"
    assert "MacroQuest.exe" in result.output


def test_cli_run_unknown_target_errors(stub_init):
    result = runner.invoke(main.app, ["run", "bogus"])
    assert result.exit_code == 2
    assert "Unknown shortcut" in result.output


def test_cli_run_launch_failure_exits_1(stub_init):
    def boom(r, extra=None):
        raise FileNotFoundError("MacroQuest.exe not found in the specified folder.")
    stub_init.setattr(shortcuts, "run", boom)
    result = runner.invoke(main.app, ["run", "vvmq"])
    assert result.exit_code == 1
    assert "Couldn't run vvmq" in result.output


def test_cli_run_bare_lists(stub_init):
    stub_init.setattr(shortcuts, "runnable_available", lambda r: True)
    result = runner.invoke(main.app, ["run"])
    assert result.exit_code == 0, result.output
    assert "vvmq" in result.output and "eqgame" in result.output


def test_cli_server_override_is_applied(stub_init):
    from types import SimpleNamespace
    envs = []
    stub_init.setattr(config, "settings", SimpleNamespace(ENV="LIVE"))
    stub_init.setattr(config, "select_environment_in_memory", lambda env: envs.append(env))
    stub_init.setattr(shortcuts, "run", lambda r, extra=None: None)
    result = runner.invoke(main.app, ["run", "vvmq", "-s", "emu"])
    assert result.exit_code == 0, result.output
    assert envs == ["EMU"]


def test_cli_open_dispatch(stub_init):
    opened = []
    stub_init.setattr(shortcuts, "open_target",
                      lambda o: opened.append(o) or "with Notepad")
    result = runner.invoke(main.app, ["open", "config"])
    assert result.exit_code == 0, result.output
    assert opened and opened[0].key == "config"
    assert "Opened config with Notepad" in result.output
