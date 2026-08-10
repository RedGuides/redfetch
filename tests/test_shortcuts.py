"""Tests for the shared shortcuts registry (redfetch run / open)."""

import os
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from redfetch import shortcuts, processes, config, main, servers, utils
from redfetch.utils import FilteredLaunch, LaunchCommand

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
    assert shortcuts.find_runnable("mq").startup is not None
    assert shortcuts.find_runnable("eqbcs").startup is None
    assert shortcuts.find_runnable("eqgame").args == ("patchme",)
    assert shortcuts.find_runnable("meshgenerator").executable == "MeshGenerator.exe"
    assert shortcuts.find_runnable("mesh").executable == "MeshGenerator.exe"  # alias preserved
    assert shortcuts.find_runnable("meshgen").prepare is shortcuts._seed_meshgen_ini
    assert shortcuts.find_openable("settings").filename == "settings.local.toml"
    # one string in two places, so "eqhosts.txt" can never land in only one of them
    assert shortcuts.find_openable("eqhost").filename == shortcuts.EQHOST_FILENAME
    assert shortcuts.find_openable("mq-config").css == "file"
    assert shortcuts.find_openable("downloads").filename is None  # a folder


# --- run() dispatch ---------------------------------------------------------

def test_run_passes_resolved_dir_and_merged_args(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args, new_console=False:
                            calls.append((folder, exe, args, new_console)))
    r = shortcuts.Runnable("t", "L", "Foo.exe", lambda: "C:/x", args=("a",))
    shortcuts.run(r, extra=["b"])
    assert calls == [("C:/x", "Foo.exe", ["a", "b"], False)]


def test_run_invokes_prepare_hook_before_launch(monkeypatch):
    events = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args, new_console=False: events.append("run"))
    r = shortcuts.Runnable("t", "L", "Foo.exe", lambda: "C:/x",
                           prepare=lambda: events.append("prepare"))
    shortcuts.run(r)
    assert events == ["prepare", "run"]


def test_vvmq_startup_hook_late_binds(monkeypatch):
    monkeypatch.setattr(shortcuts, "start_vvmq", lambda: "sentinel")
    assert shortcuts.find_runnable("vvmq").startup() == "sentinel"


# --- per-server entries: name and label resolve at read time -----------------

def _dynamic(exe, label="", *, folder="C:/x", **kwargs):
    return shortcuts.Runnable(
        "dyn", "Static label", "", lambda: folder,
        resolve_executable=lambda: exe, resolve_label=lambda: label, **kwargs,
    )


def test_static_entries_resolve_from_their_own_fields():
    r = shortcuts.find_runnable("eqbcs")
    assert shortcuts.runnable_executable(r) == "EQBCS.exe"
    assert shortcuts.runnable_label(r) == r.label
    assert shortcuts.runnable_visible(r) is True


def test_dynamic_entry_resolves_its_name_and_label():
    r = _dynamic("LazarusPatcherCLI.exe", "Project Lazarus patcher")
    assert shortcuts.runnable_executable(r) == "LazarusPatcherCLI.exe"
    assert shortcuts.runnable_label(r) == "Project Lazarus patcher"
    assert shortcuts.runnable_visible(r) is True


def test_dynamic_entry_with_nothing_behind_it_hides():
    r = _dynamic("", "")
    assert shortcuts.runnable_visible(r) is False
    assert shortcuts.runnable_available(r) is False
    assert shortcuts.runnable_label(r) == "Static label"  # still names itself in errors


def test_dynamic_availability_follows_the_resolved_name(tmp_path):
    (tmp_path / "LazarusPatcherCLI.exe").write_bytes(b"MZ")
    folder = str(tmp_path)
    assert shortcuts.runnable_available(_dynamic("LazarusPatcherCLI.exe", folder=folder)) is True
    assert shortcuts.runnable_available(_dynamic("Other.exe", folder=folder)) is False


def test_run_launches_the_resolved_name_in_its_own_console(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args, new_console=False:
                            calls.append((exe, new_console)))
    shortcuts.run(_dynamic("Real.exe", new_console=True))
    assert calls == [("Real.exe", True)]


def test_run_refuses_an_entry_that_resolves_to_nothing(monkeypatch):
    monkeypatch.setattr(processes, "run_executable",
                        lambda *a, **k: pytest.fail("nothing to launch"))
    r = shortcuts.Runnable("patcher", "Server patcher", "", lambda: "C:/x",
                           resolve_executable=lambda: "",
                           prepare=lambda: pytest.fail("nothing to prepare"))
    with pytest.raises(ValueError, match="no patcher to run"):
        shortcuts.run(r)


# --- the patcher entry ------------------------------------------------------

def _active_server(monkeypatch, **kwargs):
    fields = dict(label="Project Lazarus", eqpath="C:/EQ",
                  patcher_url="https://laz.example.test/p.zip",
                  patcher_exe="LazarusPatcherCLI.exe")
    context = servers.ServerContext(**{**fields, **kwargs})
    monkeypatch.setattr(shortcuts, "_active_context", lambda: context)
    return context


def test_patcher_entry_names_the_active_server(monkeypatch):
    _active_server(monkeypatch)
    r = shortcuts.find_runnable("patcher")
    assert shortcuts.runnable_executable(r) == "LazarusPatcherCLI.exe"
    assert shortcuts.runnable_label(r) == "Project Lazarus patcher 🩹"
    assert shortcuts.runnable_visible(r) is True
    assert r.resolve_dir() == "C:/EQ"
    assert r.new_console is True  # a console patcher must not share the TUI's window


def test_patcher_entry_hides_without_a_server_patcher(monkeypatch):
    """Live, and the bare emu setup: no entry behind them, so no patcher_url."""
    _active_server(monkeypatch, label="EverQuest Live", patcher_url="", patcher_exe="")
    r = shortcuts.find_runnable("patcher")
    assert shortcuts.runnable_visible(r) is False
    assert shortcuts.runnable_available(r) is False
    assert shortcuts.runnable_label(r) == "Server patcher 🩹"


def test_patcher_entry_rejects_a_hostile_exe_name(monkeypatch):
    """A custom server's exe name is user-authored, so running it uses the bootstrap's gate."""
    _active_server(monkeypatch, patcher_exe="..\\..\\Windows\\System32\\calc.exe")
    r = shortcuts.find_runnable("patcher")
    assert shortcuts.runnable_executable(r) == ""
    assert shortcuts.runnable_visible(r) is False


def test_patcher_entry_reads_the_global_env(monkeypatch):
    """--server rewrites config.settings.ENV in memory; the shortcut has to follow it."""
    seen = []
    monkeypatch.setattr(config, "settings", SimpleNamespace(ENV="EMU"))
    monkeypatch.setattr(
        shortcuts.servers, "active_server_context",
        lambda env: seen.append(env) or servers.ServerContext(label="X", eqpath="C:/EQ"),
    )
    shortcuts._active_context()
    assert seen == ["EMU"]


# --- run_executable: console windows ----------------------------------------

def _fake_popen(monkeypatch):
    """Stand in for subprocess so the creation flag is assertable off Windows."""
    calls = []
    monkeypatch.setattr(processes, "IS_WINDOWS", True)
    monkeypatch.setattr(processes, "subprocess", SimpleNamespace(
        CREATE_NEW_CONSOLE=0x10,
        Popen=lambda argv, **kwargs: calls.append(kwargs),
    ))
    return calls


def test_new_console_reaches_popen(monkeypatch, tmp_path):
    (tmp_path / "Patcher.exe").write_bytes(b"MZ")
    calls = _fake_popen(monkeypatch)
    processes.run_executable(str(tmp_path), "Patcher.exe", new_console=True)
    assert calls[0]["creationflags"] == 0x10


def test_gui_executables_keep_the_shared_console(monkeypatch, tmp_path):
    (tmp_path / "MacroQuest.exe").write_bytes(b"MZ")
    calls = _fake_popen(monkeypatch)
    processes.run_executable(str(tmp_path), "MacroQuest.exe")
    assert calls[0]["creationflags"] == 0


# --- launch_loadout(): the shared companion routine --------------------------

def test_launch_loadout_empty_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(shortcuts.utils, "resolve_post_update_launch_filtered",
                        lambda env=None, running=None: FilteredLaunch([], []))
    assert shortcuts.launch_loadout(frozenset()) == []


def test_launch_loadout_reports_launches_skips_and_failures_in_order(monkeypatch):
    def _run(command, cwd=None):
        if command == "bad --flag":
            raise FileNotFoundError("bad")
        return True

    monkeypatch.setattr(shortcuts.processes, "run_command", _run)
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: FilteredLaunch(
            [LaunchCommand(["C:\\x\\EQBCS.exe"], "C:\\x"), LaunchCommand("bad --flag")],
            ["C:\\y\\MySEQ.exe"],
        ),
    )
    messages = shortcuts.launch_loadout(frozenset())
    assert messages == [
        shortcuts.LaunchMessage("MySEQ.exe is already running; not starting another."),
        shortcuts.LaunchMessage("EQBCS.exe started."),
        shortcuts.LaunchMessage("Failed to start bad: bad", is_error=True),
    ]


# --- start_vvmq(): full startup (MacroQuest + companion loadout) -------------

@pytest.fixture
def vvmq_env(monkeypatch):
    calls = {"started": [], "commands": []}
    monkeypatch.setattr(shortcuts.sys, "platform", "win32")
    monkeypatch.setattr(shortcuts.utils, "get_vvmq_path", lambda: "C:\\VanillaMQ")
    monkeypatch.setattr(shortcuts.utils, "should_offer_mq_start", lambda running=None: True)
    monkeypatch.setattr(
        shortcuts.processes, "run_executable",
        lambda folder, exe, *a, **k: calls["started"].append((folder, exe)) or True,
    )
    monkeypatch.setattr(
        shortcuts.processes, "run_command",
        lambda command, cwd=None: calls["commands"].append((command, cwd)) or True,
    )
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: FilteredLaunch([], []),
    )
    return monkeypatch, calls


def _start(running=frozenset()):
    return shortcuts.start_vvmq(running=running)


def test_start_vvmq_starts_mq_then_loadout(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: FilteredLaunch(
            [LaunchCommand(["C:\\VanillaMQ\\EQBCS.exe"], "C:\\VanillaMQ")], []
        ),
    )
    result = _start()
    assert result.mq_up is True
    assert calls["started"] == [("C:\\VanillaMQ", "MacroQuest.exe")]
    assert calls["commands"] == [(["C:\\VanillaMQ\\EQBCS.exe"], "C:\\VanillaMQ")]
    assert ("MacroQuest started.", False) in result.messages
    assert ("EQBCS.exe started.", False) in result.messages


def test_start_vvmq_skips_mq_when_running_but_still_launches_loadout(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(shortcuts.utils, "should_offer_mq_start", lambda running=None: False)
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: FilteredLaunch(
            [LaunchCommand(["C:\\VanillaMQ\\EQBCS.exe"], "C:\\VanillaMQ")], []
        ),
    )
    result = _start()
    assert result.mq_up is True
    assert calls["started"] == []
    assert calls["commands"] == [(["C:\\VanillaMQ\\EQBCS.exe"], "C:\\VanillaMQ")]
    assert ("MacroQuest is already running; not starting another.", False) in result.messages


def test_start_vvmq_missing_path_is_error(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(shortcuts.utils, "get_vvmq_path", lambda: None)
    result = _start()
    assert result.mq_up is False
    assert calls["started"] == [] and calls["commands"] == []
    assert any("path not found" in msg and err for msg, err in result.messages)


def test_start_vvmq_mq_failure_skips_loadout(vvmq_env):
    monkeypatch, calls = vvmq_env

    def _boom(folder, exe, *a, **k):
        raise FileNotFoundError("MacroQuest.exe not found")

    monkeypatch.setattr(shortcuts.processes, "run_executable", _boom)
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: pytest.fail("loadout must not launch when MacroQuest fails"),
    )
    result = _start()
    assert result.mq_up is False
    assert calls["commands"] == []
    assert any("Failed to start MacroQuest" in msg and err for msg, err in result.messages)


def test_start_vvmq_reports_already_running_companion(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: FilteredLaunch([], ["C:\\y\\MySEQ.exe"]),
    )
    result = _start()
    assert result.mq_up is True
    assert calls["commands"] == []
    assert any("MySEQ.exe is already running" in msg for msg, _ in result.messages)


def test_start_vvmq_companion_failure_continues(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(
        shortcuts.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: FilteredLaunch(
            [LaunchCommand("bad --flag"), LaunchCommand(["C:\\VanillaMQ\\EQBCS.exe"], "C:\\VanillaMQ")], []
        ),
    )

    def _run(command, cwd=None):
        if command == "bad --flag":
            raise FileNotFoundError("bad")
        calls["commands"].append((command, cwd))
        return True

    monkeypatch.setattr(shortcuts.processes, "run_command", _run)
    result = _start()
    assert result.mq_up is True
    assert calls["commands"] == [(["C:\\VanillaMQ\\EQBCS.exe"], "C:\\VanillaMQ")]
    assert any("Failed to start bad" in msg and err for msg, err in result.messages)


def _spy_helpers(monkeypatch, seen):
    """Patch the two helpers that receive ``running``, recording what they get."""
    def _offer(running=None):
        seen["offer"] = running
        return True

    def _resolve(env=None, running=None):
        seen["resolve"] = running
        return FilteredLaunch([], [])

    monkeypatch.setattr(shortcuts.utils, "should_offer_mq_start", _offer)
    monkeypatch.setattr(shortcuts.utils, "resolve_post_update_launch_filtered", _resolve)


def test_start_vvmq_scans_running_once_when_not_given(vvmq_env):
    monkeypatch, calls = vvmq_env
    scanned = frozenset({"scanned.exe"})
    scans = []
    seen = {}
    monkeypatch.setattr(shortcuts.processes, "running_executable_paths",
                        lambda: scans.append(1) or scanned)
    _spy_helpers(monkeypatch, seen)

    shortcuts.start_vvmq()

    assert scans == [1]
    assert seen["offer"] is scanned and seen["resolve"] is scanned


def test_start_vvmq_forwards_running_to_helpers(vvmq_env):
    monkeypatch, calls = vvmq_env
    seen = {}
    _spy_helpers(monkeypatch, seen)
    sentinel = frozenset({"given.exe"})

    shortcuts.start_vvmq(running=sentinel)

    assert seen["offer"] is sentinel and seen["resolve"] is sentinel


def test_start_vvmq_loadout_resolution_failure_bubbles(vvmq_env):
    monkeypatch, calls = vvmq_env

    def _boom(env=None, running=None):
        raise TypeError("POST_UPDATE_LAUNCH command must be a string or list.")

    monkeypatch.setattr(shortcuts.utils, "resolve_post_update_launch_filtered", _boom)
    with pytest.raises(TypeError):
        _start()


def test_start_vvmq_scan_failure_bubbles(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(shortcuts.processes, "running_executable_paths",
                        lambda: (_ for _ in ()).throw(OSError("psutil boom")))
    with pytest.raises(OSError):
        shortcuts.start_vvmq()
    assert calls["started"] == [] and calls["commands"] == []


def test_start_vvmq_non_windows_is_clean_error(vvmq_env):
    monkeypatch, calls = vvmq_env
    monkeypatch.setattr(shortcuts.sys, "platform", "linux")
    result = _start()

    assert result.mq_up is False
    assert calls["started"] == [] and calls["commands"] == []
    assert any(err and "only supported on Windows" in msg for msg, err in result.messages)


def test_start_vvmq_routes_companions_through_shared_loadout(vvmq_env):
    monkeypatch, calls = vvmq_env
    sentinel = shortcuts.LaunchMessage("sentinel companion")
    seen = {}

    def _loadout(running=None):
        seen["running"] = running
        return [sentinel]

    monkeypatch.setattr(shortcuts, "launch_loadout", _loadout)
    result = _start(running=frozenset({"x"}))

    assert sentinel in result.messages
    assert seen["running"] == frozenset({"x"})


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
    (tmp_path / "f.ini").touch()
    present = shortcuts.Openable("t", "L", lambda: str(tmp_path), "f.ini")
    assert shortcuts.openable_available(present) is True
    absent = shortcuts.Openable("t", "L", lambda: str(tmp_path), "missing.ini")
    assert shortcuts.openable_available(absent) is False


# --- the eqhost.txt reader and its tooltip ----------------------------------

LAZ = "login.eqemulator.net:5999"


def _eqhost(tmp_path, data):
    path = tmp_path / shortcuts.EQHOST_FILENAME
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


@pytest.mark.parametrize("body", [
    f"[LoginServer]\nHost={LAZ}\n",
    f"[LoginServer]\r\nHost={LAZ}\r\n",                     # CRLF, what a client ships
    f"[LoginServer]\n\tHost\t=\t{LAZ}\t\n",
    f"[loginserver]\nhost={LAZ}\n",
    f"\ufeff[LoginServer]\nHost={LAZ}\n",                   # a BOM from Notepad
    f"[LoginServer]\n#Host=old.login:5999\nHost={LAZ}\n",   # a commented-out old host
    f"Host={LAZ}\n",                                        # no header at all
])
def test_the_reader_finds_the_active_host(tmp_path, body):
    _eqhost(tmp_path, body)
    assert shortcuts.read_login_server(str(tmp_path)) == LAZ


@pytest.mark.parametrize("body", [
    "",
    "[LoginServer]\n",
    "[LoginServer]\nHost\n",
    "[LoginServer]\nHost=\n",
    "[LoginServer]\n#Host=old.login:5999\n",   # commented out is not active
    "[LoginServer]\n;Host=old.login:5999\n",
])
def test_a_file_with_no_active_host_reads_as_empty(tmp_path, body):
    _eqhost(tmp_path, body)
    assert shortcuts.read_login_server(str(tmp_path)) == ""


def test_unreadable_files_read_as_empty_instead_of_raising(tmp_path):
    assert shortcuts.read_login_server(str(tmp_path)) == ""   # no file at all
    (tmp_path / shortcuts.EQHOST_FILENAME).mkdir()            # a directory of that name
    assert shortcuts.read_login_server(str(tmp_path)) == ""


def test_a_utf16_file_does_not_raise(tmp_path):
    """errors='replace' matters: a tooltip refresh must never explode on this."""
    _eqhost(tmp_path, f"[LoginServer]\nHost={LAZ}\n".encode("utf-16"))
    assert shortcuts.read_login_server(str(tmp_path)) == ""


def test_an_ansi_byte_does_not_raise(tmp_path):
    _eqhost(tmp_path, b"; caf\xe9\r\n[LoginServer]\r\nHost=" + LAZ.encode() + b"\r\n")
    assert shortcuts.read_login_server(str(tmp_path)) == LAZ


@pytest.mark.parametrize("folder", ["", "   ", None])
def test_reading_without_a_folder_never_touches_the_working_directory(tmp_path, monkeypatch, folder):
    monkeypatch.chdir(tmp_path)
    _eqhost(tmp_path, f"[LoginServer]\nHost={LAZ}\n")
    assert shortcuts.read_login_server(folder) == ""


def _at_eq_dir(monkeypatch, folder):
    # Not shortcuts._eq_dir: the registry captured that function at import, so patching
    # the name would leave resolve_dir pointing at the real one.
    monkeypatch.setattr(config, "active_settings", lambda: {"EQPATH": str(folder)})


def test_the_eqhost_tooltip_names_the_login_server(tmp_path, monkeypatch):
    _eqhost(tmp_path, f"[LoginServer]\nHost={LAZ}\n")
    _at_eq_dir(monkeypatch, tmp_path)
    assert LAZ in shortcuts.openable_tooltip(shortcuts.find_openable("eqhost"))


def test_the_eqhost_tooltip_falls_back_when_there_is_nothing_to_read(tmp_path, monkeypatch):
    _at_eq_dir(monkeypatch, tmp_path)
    openable = shortcuts.find_openable("eqhost")
    assert shortcuts.openable_tooltip(openable) == openable.tooltip


def test_a_plain_openable_keeps_its_static_tooltip():
    o = shortcuts.Openable("t", "L", lambda: "C:/d", tooltip="Static.")
    assert shortcuts.openable_tooltip(o) == "Static."


# --- CLI: `redfetch run` / `redfetch open` ----------------------------------

def test_cli_run_launches(stub_init):
    launched = []
    stub_init.setattr(shortcuts, "run", lambda r, extra=None: launched.append(r))
    result = runner.invoke(main.app, ["run", "eqbcs"])
    assert result.exit_code == 0, result.output
    assert launched and launched[0].key == "eqbcs"
    assert "EQBCS.exe" in result.output


def test_cli_run_vvmq_does_full_startup(stub_init):
    stub_init.setattr(shortcuts, "run", lambda r, extra=None: pytest.fail("vvmq must not use bare run()"))
    stub_init.setattr(
        shortcuts, "start_vvmq",
        lambda: shortcuts.StartupResult([("MacroQuest started.", False), ("EQBCS started.", False)], mq_up=True),
    )
    result = runner.invoke(main.app, ["run", "vvmq"])
    assert result.exit_code == 0, result.output
    assert "MacroQuest started." in result.output
    assert "EQBCS started." in result.output


def test_cli_run_vvmq_failure_exits_1(stub_init):
    stub_init.setattr(
        shortcuts, "start_vvmq",
        lambda: shortcuts.StartupResult([("MacroQuest path not found. Please check your configuration.", True)], mq_up=False),
    )
    result = runner.invoke(main.app, ["run", "vvmq"])
    assert result.exit_code == 1
    assert "MacroQuest path not found" in result.output


def test_cli_run_vvmq_companion_failure_still_exits_0(stub_init):
    stub_init.setattr(
        shortcuts, "start_vvmq",
        lambda: shortcuts.StartupResult(
            [("MacroQuest started.", False), ("Failed to start EQBCS.exe: boom", True)], mq_up=True
        ),
    )
    result = runner.invoke(main.app, ["run", "vvmq"])
    assert result.exit_code == 0, result.output
    assert "Failed to start EQBCS.exe" in result.output


def test_cli_run_unknown_target_errors(stub_init):
    result = runner.invoke(main.app, ["run", "bogus"])
    assert result.exit_code == 2
    assert "Unknown shortcut" in result.output


def test_cli_run_launch_failure_exits_1(stub_init):
    def boom(r, extra=None):
        raise FileNotFoundError("EQBCS.exe not found in the specified folder.")
    stub_init.setattr(shortcuts, "run", boom)
    result = runner.invoke(main.app, ["run", "eqbcs"])
    assert result.exit_code == 1
    assert "Couldn't run eqbcs" in result.output


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
    result = runner.invoke(main.app, ["run", "eqbcs", "-s", "emu"])
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
