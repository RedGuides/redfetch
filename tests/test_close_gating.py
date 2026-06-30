"""Tests for the 'only close MacroQuest when an update needs it' gate:

- ``sync.download_target_dirs`` collects the folders an update will write to.
- ``processes.find_processes_locking_dirs`` reports running processes whose loaded modules
  live under those folders (the real collision), and stays conservative when it can't look.
"""

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


def _install_processes(monkeypatch, mq_folder, candidates, procs):
    """candidates -> are_executables_running_in_folder result; procs -> pid->_FakeProc."""
    monkeypatch.setattr(
        processes, "are_executables_running_in_folder", lambda folder: list(candidates)
    )
    monkeypatch.setattr(processes, "get_eqgame_process_pids", lambda: [])
    monkeypatch.setattr(processes.psutil, "Process", lambda pid: procs[pid])


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

    assert processes.find_processes_locking_dirs(mq, [mq]) == [(200, exe)]


def test_access_denied_is_treated_as_collision(fake_windows, tmp_path):
    mq = str(tmp_path / "VanillaMQ")
    lua = str(tmp_path / "VanillaMQ" / "lua")
    exe = os.path.join(mq, "MacroQuest.exe")
    proc = _FakeProc(100, [], exe=exe, deny_maps=True)
    _install_processes(fake_windows, mq, [(100, exe)], {100: proc})

    # Even targeting only lua, an unreadable process is reported (safe fallback).
    assert processes.find_processes_locking_dirs(mq, [lua]) == [(100, exe)]
