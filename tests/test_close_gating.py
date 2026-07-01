"""Tests for closing only processes that can block an update."""

import asyncio
import os

import psutil
import pytest

from redfetch import processes, sync
from redfetch.sync_types import ExecutionPlan, PlannedAction


def _action(resource_id, *, action, reason, resolved_path=None):
    return PlannedAction(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        parent_id=None,
        parent_target_key=None,
        root_resource_id=resource_id,
        target_kind="root",
        action=action,
        reason=reason,
        resolved_path=resolved_path,
    )


def _plan(*actions):
    return ExecutionPlan(actions={a.target_key: a for a in actions})


# --- download_target_dirs -------------------------------------------------------------

def test_download_target_dirs_collects_only_download_destinations(tmp_path):
    plugins = tmp_path / "plugins"
    lua = tmp_path / "lua"
    plugins.mkdir()
    lua.mkdir()

    plan = _plan(
        _action("11", action="download", reason="outdated", resolved_path=str(plugins)),
        _action("25", action="download", reason="outdated", resolved_path=str(lua)),
        # skip / no resolved_path must not contribute a destination
        _action("10", action="skip", reason="already_current", resolved_path=str(tmp_path / "x")),
        _action("12", action="download", reason="outdated", resolved_path=None),
    )

    assert sync.download_target_dirs(plan) == {str(plugins), str(lua)}


# --- find_processes_locking_dirs ------------------------------------------------------

class _FakeMap:
    def __init__(self, path):
        self.path = path


class _FakeProc:
    def __init__(self, pid, mapped_paths, *, exe="MacroQuest.exe", deny_maps=False):
        self.pid = pid
        self._mapped = mapped_paths
        self._exe = exe
        self._deny = deny_maps

    def memory_maps(self):
        if self._deny:
            raise psutil.AccessDenied(self.pid)
        return [_FakeMap(p) for p in self._mapped]

    def exe(self):
        return self._exe


@pytest.fixture
def fake_windows(monkeypatch):
    """Force the Windows-only branch and stub the process-discovery helpers."""
    monkeypatch.setattr(processes, "IS_WINDOWS", True)
    return monkeypatch


def _install_processes(monkeypatch, mq_folder, candidates, procs, iter_procs=()):
    monkeypatch.setattr(
        processes, "are_executables_running_in_folder", lambda folder: list(candidates)
    )
    monkeypatch.setattr(processes, "get_eqgame_process_pids", lambda: [])
    monkeypatch.setattr(processes.psutil, "Process", lambda pid: procs[pid])
    monkeypatch.setattr(processes.psutil, "process_iter", lambda attrs=None: list(iter_procs))


def test_no_collision_when_modules_outside_targets(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    plugins = str(tmp_path / "VanillaMQ" / "plugins")
    lua = str(tmp_path / "VanillaMQ" / "lua")

    proc = _FakeProc(100, [os.path.join(mq, "MacroQuest.exe"), os.path.join(plugins, "MQ2Foo.dll")])
    _install_processes(fake_windows, mq, [(100, os.path.join(mq, "MacroQuest.exe"))], {100: proc})

    # An update that only writes into the lua folder collides with nothing loaded.
    assert processes.find_processes_locking_dirs(mq, [lua]) == []


def test_collision_when_module_under_target(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    plugins = str(tmp_path / "VanillaMQ" / "plugins")

    exe = os.path.join(mq, "MacroQuest.exe")
    proc = _FakeProc(100, [exe, os.path.join(plugins, "MQ2Foo.dll")])
    _install_processes(fake_windows, mq, [(100, exe)], {100: proc})

    assert processes.find_processes_locking_dirs(mq, [plugins]) == [(100, exe)]


def test_eqgame_process_is_included_as_candidate(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    exe = os.path.join("C:\\", "EQ", "eqgame.exe")
    # eqgame (in the EQ folder) maps an MQ dll from the VanillaMQ folder.
    proc = _FakeProc(200, [os.path.join(mq, "mq2main.dll")], exe=exe)

    fake_windows.setattr(processes, "are_executables_running_in_folder", lambda folder: [])
    fake_windows.setattr(processes, "get_eqgame_process_pids", lambda: [200])
    fake_windows.setattr(processes.psutil, "Process", lambda pid: proc)
    fake_windows.setattr(processes.psutil, "process_iter", lambda attrs=None: [])

    assert processes.find_processes_locking_dirs(mq, [mq]) == [(200, exe)]


def test_access_denied_is_treated_as_collision(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    lua = str(tmp_path / "VanillaMQ" / "lua")
    exe = os.path.join(mq, "MacroQuest.exe")
    proc = _FakeProc(100, [], exe=exe, deny_maps=True)
    _install_processes(fake_windows, mq, [(100, exe)], {100: proc})

    # Even targeting only lua, an unreadable process is reported (safe fallback).
    assert processes.find_processes_locking_dirs(mq, [lua]) == [(100, exe)]


# --- exe-under-target detection (MySEQ/EQBCS/custom living in a folder we update) ------

class _IterProc:
    def __init__(self, pid, exe):
        self.pid = pid
        self.info = {"exe": exe}


def test_app_running_from_target_folder_is_collision(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    myseq_dir = str(tmp_path / "MySEQ")
    myseq_exe = os.path.join(myseq_dir, "MySEQ.exe")

    _install_processes(
        fake_windows, mq, candidates=[], procs={},
        iter_procs=[_IterProc(300, myseq_exe)],
    )

    assert processes.find_processes_locking_dirs(mq, [myseq_dir]) == [(300, myseq_exe)]


def test_app_outside_target_folders_is_ignored(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    target = str(tmp_path / "MySEQ")
    other_exe = os.path.join(str(tmp_path / "Unrelated"), "Notepad.exe")

    _install_processes(
        fake_windows, mq, candidates=[], procs={},
        iter_procs=[_IterProc(400, other_exe)],
    )

    assert processes.find_processes_locking_dirs(mq, [target]) == []


# --- terminate_processes: close colliders; go folder-wide & unload when MQ is involved ---

class _KillableProc:
    def __init__(self, pid, sink):
        self.pid = pid
        self._sink = sink

    def terminate(self):
        self._sink.append(self.pid)

    def wait(self, timeout=None):
        pass


def _install_terminate(monkeypatch, *, eqgame=(), folder_exes=()):
    killed, unloaded = [], []
    monkeypatch.setattr(processes, "get_eqgame_process_pids", lambda: list(eqgame))
    monkeypatch.setattr(
        processes, "are_executables_running_in_folder", lambda folder: list(folder_exes)
    )
    monkeypatch.setattr(processes.psutil, "Process", lambda pid: _KillableProc(pid, killed))
    monkeypatch.setattr(processes, "force_remote_unload", lambda: unloaded.append(True))
    return killed, unloaded


def test_terminate_non_mq_collider_closes_only_it(fake_windows):
    killed, unloaded = _install_terminate(
        fake_windows, folder_exes=[(500, "C:\\VanillaMQ\\MacroQuest.exe")]
    )

    assert processes.terminate_processes([(100, "C:\\MySEQ\\MySEQ.exe")], "C:\\VanillaMQ") is None

    assert killed == [100]
    assert unloaded == []


def test_terminate_mq_collider_closes_whole_folder_and_unloads(fake_windows):
    killed, unloaded = _install_terminate(
        fake_windows,
        folder_exes=[(100, "C:\\VanillaMQ\\MacroQuest.exe"), (101, "C:\\VanillaMQ\\EQBCS.exe")],
    )

    processes.terminate_processes([(100, "C:\\VanillaMQ\\MacroQuest.exe")], "C:\\VanillaMQ")

    assert sorted(killed) == [100, 101]
    assert unloaded == [True]


def test_terminate_eqgame_unloaded_not_killed(fake_windows):
    killed, unloaded = _install_terminate(
        fake_windows, eqgame=[200], folder_exes=[(100, "C:\\VanillaMQ\\MacroQuest.exe")]
    )

    processes.terminate_processes(
        [(100, "C:\\VanillaMQ\\MacroQuest.exe"), (200, "eqgame.exe")], "C:\\VanillaMQ"
    )

    assert killed == [100]
    assert unloaded == [True]


# --- running_executable_paths (single scan, no DLL inspection) -------------------------

class _FakeIterProc:
    def __init__(self, exe):
        self.info = {"exe": exe}


def test_running_executable_paths_normalizes_and_dedupes(monkeypatch):
    procs = [
        _FakeIterProc("C:\\VanillaMQ\\EQBCS.exe"),
        _FakeIterProc("C:\\VanillaMQ\\eqbcs.exe"),  # same path, different case
        _FakeIterProc(None),  # no exe -> ignored
    ]
    monkeypatch.setattr(processes.psutil, "process_iter", lambda attrs: procs)

    paths = processes.running_executable_paths()
    assert os.path.normcase(os.path.normpath("C:\\VanillaMQ\\EQBCS.exe")) in paths
    assert len(paths) == 1  # case-insensitive dedupe, None skipped


# --- resolve_post_update_launch_filtered ----------------------------------------------

from redfetch import utils


def test_filter_skips_running_preset_keeps_idle(monkeypatch, tmp_path):
    eqbcs = tmp_path / "EQBCS.exe"
    myseq = tmp_path / "MySEQ.exe"
    eqbcs.write_text("")
    myseq.write_text("")

    monkeypatch.setattr(
        utils, "resolve_post_update_launch",
        lambda env=None: [([str(eqbcs)], str(tmp_path)), ([str(myseq)], str(tmp_path))],
    )
    # EQBCS is already running; MySEQ is not.
    monkeypatch.setattr(
        processes, "running_executable_paths",
        lambda: {os.path.normcase(os.path.normpath(str(eqbcs)))},
    )

    to_run, skipped = utils.resolve_post_update_launch_filtered("LIVE")
    assert to_run == [([str(myseq)], str(tmp_path))]
    assert skipped == [str(eqbcs)]


def test_filter_always_runs_non_file_program(monkeypatch, tmp_path):
    # A custom powershell command: program token is "powershell", not a file on disk.
    custom = "powershell -NoProfile -File C:\\scripts\\go.ps1"
    monkeypatch.setattr(
        utils, "resolve_post_update_launch", lambda env=None: [(custom, None)]
    )
    # Even if "powershell" happened to be a running exe name, a non-file program is never skipped.
    monkeypatch.setattr(processes, "running_executable_paths", lambda: set())

    to_run, skipped = utils.resolve_post_update_launch_filtered("LIVE")
    assert to_run == [(custom, None)]
    assert skipped == []


# --- prompt_auto_run_macroquest gates the "also start" programs ------------------------

from redfetch import main


class _AutoRunSettings:
    ENV = "LIVE"

    def __init__(self, value):
        self._value = value

    def from_env(self, env):
        return {"AUTO_RUN_VVMQ": self._value}


@pytest.fixture
def mq_env(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.utils, "get_vvmq_path", lambda: "C:\\VanillaMQ")
    launches = []
    monkeypatch.setattr(
        main.processes, "run_executable",
        lambda folder, exe, *a, **k: (launches.append((folder, exe)), True)[1],
    )

    def _set_auto_run(value):
        monkeypatch.setattr(main.config, "settings", _AutoRunSettings(value))

    return launches, _set_auto_run


def test_auto_run_true_starts_mq_and_returns_true(mq_env):
    launches, set_auto = mq_env
    set_auto(True)
    assert main.prompt_auto_run_macroquest() is True
    assert launches == [("C:\\VanillaMQ", "MacroQuest.exe")]


def test_auto_run_false_does_not_start_mq(mq_env):
    launches, set_auto = mq_env
    set_auto(False)
    assert main.prompt_auto_run_macroquest() is False
    assert launches == []


@pytest.mark.parametrize("choice, expected", [("yes", True), ("no", False), ("never", False)])
def test_auto_run_prompt_choice_gates_start(mq_env, monkeypatch, choice, expected):
    launches, set_auto = mq_env
    set_auto(None)  # unconfigured -> prompts the user
    monkeypatch.setattr(main, "Prompt", type("P", (), {"ask": staticmethod(lambda *a, **k: choice)}))
    monkeypatch.setattr(main.config, "update_setting", lambda *a, **k: None)

    assert main.prompt_auto_run_macroquest() is expected
    assert (launches != []) is expected


# --- is_executable_running ------------------------------------------------------------

def test_is_executable_running(monkeypatch):
    target = "C:\\App\\Foo.exe"
    monkeypatch.setattr(
        processes, "running_executable_paths",
        lambda: {os.path.normcase(os.path.normpath(target))},
    )
    assert processes.is_executable_running("C:/App/Foo.exe") is True
    assert processes.is_executable_running("C:\\App\\Bar.exe") is False


# --- post-update gate in handle_download_watched_async --------------------------------
# The loadout runs only after MacroQuest actually starts.

_MQ = "C:\\VanillaMQ\\MacroQuest.exe"


def _run_gate(monkeypatch, *, mq_running, mq_starts=False, mq_path=_MQ):
    rec = {"prompted": 0, "loadout": 0}

    async def _no(*a, **k):
        return False
    async def _run_sync(*a, **k):
        return True

    monkeypatch.setattr(main.utils.sys, "platform", "win32")  # should_offer_mq_start is win32-gated
    monkeypatch.setattr(main.net, "is_mq_down", _no)
    monkeypatch.setattr(main.utils, "get_base_path", lambda: "C:\\VanillaMQ")
    monkeypatch.setattr(main.utils, "macroquest_exe_path", lambda: mq_path)
    monkeypatch.setattr(main, "prompt_navmesh_opt_in", lambda: None)
    monkeypatch.setattr(main.processes, "running_executable_paths", lambda: set())
    monkeypatch.setattr(main.processes, "is_executable_running", lambda p, running=None: mq_running)
    monkeypatch.setattr(main.sync, "run_sync", _run_sync)

    def _prompt():
        rec["prompted"] += 1
        return mq_starts
    monkeypatch.setattr(main, "prompt_auto_run_macroquest", _prompt)
    monkeypatch.setattr(
        main, "run_post_update_launch",
        lambda running=None: rec.__setitem__("loadout", rec["loadout"] + 1),
    )

    assert asyncio.run(main.handle_download_watched_async("db", {})) is True
    return rec


def test_gate_skips_when_mq_already_running(monkeypatch):
    assert _run_gate(monkeypatch, mq_running=True) == {"prompted": 0, "loadout": 0}


def test_gate_starts_mq_then_runs_loadout_when_down(monkeypatch):
    assert _run_gate(monkeypatch, mq_running=False, mq_starts=True) == {"prompted": 1, "loadout": 1}


def test_gate_declined_runs_no_loadout(monkeypatch):
    assert _run_gate(monkeypatch, mq_running=False, mq_starts=False) == {"prompted": 1, "loadout": 0}


# --- plan_post_update_session ---------------------------------------------------------


def test_plan_runs_exactly_one_scan(monkeypatch):
    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils, "macroquest_exe_path", lambda: "C:/MQ/MacroQuest.exe")
    calls = {"n": 0}

    def _scan():
        calls["n"] += 1
        return set()  # MQ not running -> offer True

    monkeypatch.setattr(processes, "running_executable_paths", _scan)
    monkeypatch.setattr(utils, "resolve_post_update_launch", lambda env=None: [])

    offer, to_run, skipped = utils.plan_post_update_session("LIVE")
    assert offer is True and calls["n"] == 1
    assert (to_run, skipped) == ([], [])
