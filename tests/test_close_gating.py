"""Tests for the staged-update post-update flow (decide -> restart/cold-start) and its pieces."""

import asyncio
import os
import sys

import pytest

from redfetch import main, post_update, processes, sync, utils
from redfetch.sync_types import ExecutionResult, ExecutionResultItem, SyncOutcome

# for tests whose fake paths/case-folding only behave under ntpath (CI also runs on ubuntu)
windows_paths = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


@pytest.fixture
def fake_windows(monkeypatch):
    """Force the Windows-only branch in processes helpers."""
    monkeypatch.setattr(processes, "IS_WINDOWS", True)
    return monkeypatch


# --- restart_macroquest: close the MQ folder (including EQBCS), relaunch ---------------------
# EverQuest is never in the MQ folder (the user closes it), so there is no unload here.

class _KillableProc:
    def __init__(self, pid, sink):
        self.pid = pid
        self._sink = sink

    def terminate(self):
        self._sink.append(self.pid)

    def wait(self, timeout=None):
        pass


@windows_paths
def test_restart_closes_folder_including_eqbcs(fake_windows):
    # EQBCS is now closed with the rest of the folder; the loadout restarts it if opted in.
    killed = []
    folder_procs = [(100, "C:\\VanillaMQ\\LV3XGukn.exe"), (101, "C:\\VanillaMQ\\EQBCS.exe")]
    fake_windows.setattr(
        processes, "are_executables_running_in_folder",
        lambda folder: [(pid, path) for pid, path in folder_procs if pid not in killed],
    )
    fake_windows.setattr(processes, "get_eqgame_process_pids", lambda: [])
    fake_windows.setattr(processes, "_post_wm_close", lambda pids: set())
    fake_windows.setattr(processes.psutil, "Process", lambda pid: _KillableProc(pid, killed))
    fake_windows.setattr(
        processes.psutil, "wait_procs", lambda procs, timeout=None: (list(procs), [])
    )
    fake_windows.setattr(processes, "_spawned_loader_processes", lambda folder: [])
    ran = []
    fake_windows.setattr(
        processes, "run_executable", lambda folder, exe, *a, **k: ran.append((folder, exe)) or True
    )

    processes.restart_macroquest("C:\\VanillaMQ")

    assert killed == [100, 101]
    assert ran == [("C:\\VanillaMQ", "MacroQuest.exe")]


@windows_paths
def test_restart_aborts_when_eq_running(fake_windows):
    # TOCTOU backstop: EQ reappeared after the caller's gate; never terminate under a live game.
    fake_windows.setattr(processes, "get_eqgame_process_pids", lambda: [200])
    killed = []
    fake_windows.setattr(processes.psutil, "Process", lambda pid: _KillableProc(pid, killed))
    fake_windows.setattr(
        processes, "are_executables_running_in_folder",
        lambda folder: [(100, "C:\\VanillaMQ\\LV3XGukn.exe")],
    )
    ran = []
    fake_windows.setattr(processes, "run_executable", lambda *a, **k: ran.append(a) or True)

    with pytest.raises(RuntimeError, match="EverQuest"):
        processes.restart_macroquest("C:\\VanillaMQ")

    assert killed == [] and ran == []


@windows_paths
def test_restart_ignores_surviving_eqbcs(fake_windows):
    # A hung/elevated EQBCS that won't die must not abort the relaunch — it can't trip
    # MQ's loader dialog.
    killed = []
    fake_windows.setattr(processes, "get_eqgame_process_pids", lambda: [])
    fake_windows.setattr(processes, "_post_wm_close", lambda pids: set())
    fake_windows.setattr(processes, "_RESTART_LEFTOVER_GRACE_SEC", 0)
    fake_windows.setattr(
        processes, "are_executables_running_in_folder",
        lambda folder: [(101, "C:\\VanillaMQ\\EQBCS.exe")],  # survives every scan
    )
    fake_windows.setattr(processes.psutil, "Process", lambda pid: _KillableProc(pid, killed))
    fake_windows.setattr(
        processes.psutil, "wait_procs", lambda procs, timeout=None: ([], list(procs))
    )
    fake_windows.setattr(processes, "_spawned_loader_processes", lambda folder: [])
    ran = []
    fake_windows.setattr(
        processes, "run_executable", lambda folder, exe, *a, **k: ran.append((folder, exe)) or True
    )

    processes.restart_macroquest("C:\\VanillaMQ")

    assert killed == [101]  # we still tried to close it
    assert ran == [("C:\\VanillaMQ", "MacroQuest.exe")]  # but it didn't block the relaunch


# --- CLI surface adapter -------------------------------------------------------------

@pytest.fixture
def cli_surface(monkeypatch):
    surface = main._CliPostUpdate()

    def set_answers(*answers):
        """Queue Prompt.ask answers; a queued exception class is raised (Ctrl-C etc.)."""
        queue = iter(answers)
        asks = []

        def _ask(*a, **k):
            asks.append(a)
            answer = next(queue)
            if isinstance(answer, type) and issubclass(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(main, "Prompt", type("P", (), {"ask": staticmethod(_ask)}))
        return asks

    return monkeypatch, surface, set_answers


def test_cli_wait_for_eq_close_polls_until_gone(cli_surface):
    monkeypatch, surface, set_answers = cli_surface
    # eqgame present on the first check, gone on the second (user closed it after the prompt).
    pids = iter([[200], []])
    monkeypatch.setattr(main.processes, "get_eqgame_process_pids", lambda: next(pids))
    asks = set_answers("")

    assert asyncio.run(surface.wait_for_eq_close()) is True
    assert len(asks) == 1


def test_cli_wait_for_eq_close_ctrl_c_aborts(cli_surface):
    monkeypatch, surface, set_answers = cli_surface
    monkeypatch.setattr(main.processes, "get_eqgame_process_pids", lambda: [200])
    set_answers(KeyboardInterrupt)

    assert asyncio.run(surface.wait_for_eq_close()) is False


@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_cli_confirm_restart_degrades_on_no_stdin(cli_surface, interrupt):
    # headless run (piped/closed stdin): a successful update must not abort the CLI
    monkeypatch, surface, set_answers = cli_surface
    set_answers(interrupt)
    assert asyncio.run(surface.confirm_restart()) is False


@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_cli_ask_cold_start_degrades_on_no_stdin(cli_surface, interrupt):
    monkeypatch, surface, set_answers = cli_surface
    set_answers(interrupt)
    assert asyncio.run(surface.ask_cold_start()) == "no"


# --- running_executable_paths (single scan, no DLL inspection) -------------------------

class _FakeIterProc:
    def __init__(self, exe):
        self.info = {"exe": exe}


@windows_paths
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


# --- macroquest_session_running: the loader never runs as the canonical MacroQuest.exe ---

def _norm_set(*paths):
    return {os.path.normcase(os.path.normpath(p)) for p in paths}


def test_session_running_detects_loader_copy_in_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "_spawned_loader_name", lambda folder: None)
    running = _norm_set(str(tmp_path / "LV3XGukn.exe"))
    assert processes.macroquest_session_running(str(tmp_path), running) is True


@windows_paths
def test_session_running_ignores_eqbcs_alone(tmp_path, monkeypatch):
    # EQBCS runs independently of MQ; alone it must not count as a session.
    monkeypatch.setattr(processes, "_spawned_loader_name", lambda folder: None)
    running = _norm_set(str(tmp_path / "EQBCS.exe"))
    assert processes.macroquest_session_running(str(tmp_path), running) is False


@windows_paths
def test_session_running_matches_spawned_copy_outside_folder(tmp_path, monkeypatch):
    # RunFromTemp: the copy lives outside the MQ folder; found via MacroQuest.ini's name.
    monkeypatch.setattr(processes, "_spawned_loader_name", lambda folder: "Ab12Cd34.exe")
    running = _norm_set(str(tmp_path / "elsewhere" / "Ab12Cd34.exe"))
    assert processes.macroquest_session_running(str(tmp_path), running) is True


def test_session_not_running_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "_spawned_loader_name", lambda folder: "Ab12Cd34.exe")
    running = _norm_set(str(tmp_path.parent / "other" / "thing.exe"))
    assert processes.macroquest_session_running(str(tmp_path), running) is False


def test_spawned_loader_name_parsed_from_ini(tmp_path):
    (tmp_path / "MacroQuest.ini").write_text(
        "[Other]\nSpawnedProcess=wrong.exe\n[Internal]\nSpawnedProcess=Ab12Cd34.exe\n"
    )
    assert processes._spawned_loader_name(str(tmp_path)) == "Ab12Cd34.exe"
    assert processes._spawned_loader_name(str(tmp_path / "missing")) is None


# --- terminate/restart hardening --------------------------------------------------------

def test_terminate_reports_hung_process_and_continues(fake_windows):
    killed = []
    fake_windows.setattr(
        processes, "are_executables_running_in_folder",
        lambda folder: [(100, "C:\\VanillaMQ\\LV3XGukn.exe"), (101, "C:\\VanillaMQ\\EQBCS.exe")],
    )
    fake_windows.setattr(processes, "_post_wm_close", lambda pids: set())
    fake_windows.setattr(processes.psutil, "Process", lambda pid: _KillableProc(pid, killed))
    # PID 100 outlives the shared wait window: reported via the alive list, never fatal.
    fake_windows.setattr(
        processes.psutil, "wait_procs",
        lambda procs, timeout=None: (
            [p for p in procs if p.pid != 100],
            [p for p in procs if p.pid == 100],
        ),
    )

    fake_windows.setattr(processes, "_post_wm_close", lambda pids: set())
    processes.terminate_folder_processes("C:\\VanillaMQ")  # must not raise

    assert killed == [100, 101]  # the hung process didn't stop the batch


def test_terminate_wm_close_first_spares_clean_exiters(fake_windows):
    # A windowed app that obeys WM_CLOSE is never force-terminated; windowless (EQBCS) and
    # WM_CLOSE-ignoring apps still get a hard terminate.
    killed = []
    fake_windows.setattr(
        processes, "are_executables_running_in_folder",
        lambda folder: [
            (100, "C:\\VanillaMQ\\LV3XGukn.exe"),   # windowed, closes on WM_CLOSE
            (101, "C:\\VanillaMQ\\MacroQuest2.exe"),  # windowed, ignores WM_CLOSE
            (102, "C:\\VanillaMQ\\EQBCS.exe"),      # console, no window
        ],
    )
    fake_windows.setattr(processes.psutil, "Process", lambda pid: _KillableProc(pid, killed))
    # only the two windowed pids receive a WM_CLOSE
    fake_windows.setattr(processes, "_post_wm_close", lambda pids: {100, 101} & pids)

    def _wait_procs(procs, timeout=None):
        pids = {p.pid for p in procs}
        if 102 in pids:  # final wait over all targets: everyone's gone by now
            return list(procs), []
        # graceful window over the windowed subset: 100 exits, 101 hangs on
        return [p for p in procs if p.pid == 100], [p for p in procs if p.pid != 100]

    fake_windows.setattr(processes.psutil, "wait_procs", _wait_procs)

    processes.terminate_folder_processes("C:\\VanillaMQ")

    assert 100 not in killed          # closed cleanly, never force-terminated
    assert sorted(killed) == [101, 102]  # WM_CLOSE-ignorer + windowless app forced


def test_restart_refuses_relaunch_when_folder_not_clear(fake_windows):
    # An old loader copy survived termination (e.g. elevated MQ): relaunching would trip
    # MQ's blocking "exit the alternate loader" dialog, so restart must raise instead.
    fake_windows.setattr(processes, "get_eqgame_process_pids", lambda: [])
    fake_windows.setattr(processes, "_RESTART_LEFTOVER_GRACE_SEC", 0)
    fake_windows.setattr(processes, "terminate_folder_processes", lambda folder: None)
    fake_windows.setattr(processes, "_spawned_loader_processes", lambda folder: [])
    fake_windows.setattr(
        processes, "are_executables_running_in_folder",
        lambda folder: [(100, "C:\\VanillaMQ\\LV3XGukn.exe")],
    )
    ran = []
    fake_windows.setattr(processes, "run_executable", lambda *a, **k: ran.append(a) or True)

    with pytest.raises(RuntimeError):
        processes.restart_macroquest("C:\\VanillaMQ")

    assert ran == []


# --- resolve_post_update_launch_filtered ----------------------------------------------

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


# --- vvmq_was_updated: resource-level "did we write MacroQuest.exe" --------------------

def _item(resource_id, outcome):
    return ExecutionResultItem(
        target_key=f"/{resource_id}/", resource_id=resource_id, outcome=outcome, reason="outdated"
    )


@pytest.mark.parametrize(
    "items, vvmq_id, expected",
    [
        # mixed multi-item result: any() must pick the VVMQ item -> True
        ({"/60/": ("60", "downloaded"), "/25/": ("25", "downloaded")}, "60", True),
        # matching id but "skipped" (already-current) -> nothing written
        ({"/60/": ("60", "skipped")}, "60", False),
        # a downloaded non-VVMQ resource must not report the VVMQ write
        ({"/25/": ("25", "downloaded")}, "60", False),
        # no configured VVMQ id -> guard returns False
        ({"/60/": ("60", "downloaded")}, None, False),
    ],
)
def test_vvmq_was_updated(items, vvmq_id, expected):
    result = ExecutionResult(items={key: _item(*args) for key, args in items.items()})
    assert sync.vvmq_was_updated(result, vvmq_id) is expected


def test_sync_outcome_truthiness_tracks_success():
    # The compat contract for pre-existing ``if result:`` callers.
    assert bool(SyncOutcome(success=True)) is True
    assert bool(SyncOutcome(success=False, vvmq_updated=True)) is False


def test_sync_outcome_status_defaults_from_success():
    # status defaults from success so bare callers are unchanged, but busy/cancelled
    # stay distinguishable from a real failure without disturbing truthiness.
    assert SyncOutcome(success=True).status == "ok"
    assert SyncOutcome(success=False).status == "failed"
    assert SyncOutcome(success=False, status="busy").status == "busy"
    assert bool(SyncOutcome(success=False, status="cancelled")) is False


# --- post_update.decide: the resolved policy matrix -----------------------------------

@pytest.mark.parametrize(
    "success, vvmq_updated, mq_running, expected",
    [
        # a VVMQ/loader write offers to apply it; decide ignores success, so a
        # partial-failure run that still wrote VVMQ must offer (rows 3-4)
        (True, True, True, post_update.Decision.RESTART),
        (True, True, False, post_update.Decision.COLD_START),
        (False, True, True, post_update.Decision.RESTART),
        (False, True, False, post_update.Decision.COLD_START),
        # no VVMQ write -> no offer, regardless of success or mq_running
        (True, False, True, post_update.Decision.NONE),
        (True, False, False, post_update.Decision.NONE),
        (False, False, True, post_update.Decision.NONE),
        (False, False, False, post_update.Decision.NONE),
    ],
)
def test_decide(success, vvmq_updated, mq_running, expected):
    outcome = SyncOutcome(success=success, vvmq_updated=vvmq_updated)
    assert post_update.decide(outcome, mq_running=mq_running) is expected


# --- post_update.execute: the shared policy both surfaces adapt to ---------------------

class _AutoRunSettings:
    ENV = "LIVE"

    def __init__(self, value):
        self._value = value

    def from_env(self, env):
        return {"AUTO_RUN_VVMQ": self._value}


class FakeSurface:
    def __init__(self, *, restart_ok=True, cold_choice="yes", eq_close_ok=True):
        self.restart_ok = restart_ok
        self.cold_choice = cold_choice
        self.eq_close_ok = eq_close_ok
        self.notices: list[str] = []
        self.ui_synced: list[bool] = []
        self.asked_restart = 0
        self.asked_cold = 0
        self.waited_eq = 0

    def notify(self, message, *, error=False):
        self.notices.append((message, error))

    async def confirm_restart(self):
        self.asked_restart += 1
        return self.restart_ok

    async def ask_cold_start(self):
        self.asked_cold += 1
        return self.cold_choice

    def auto_run_persisted(self, value):
        self.ui_synced.append(value)

    async def wait_for_eq_close(self):
        self.waited_eq += 1
        return self.eq_close_ok


SNAPSHOT = frozenset({"snapshot.exe"})
FRESH = frozenset({"fresh.exe"})


@pytest.fixture
def exec_env(monkeypatch):
    """Windows, non-CI, MQ configured, EQ not running, AUTO_RUN_VVMQ unset."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(post_update.sys, "platform", "win32")
    monkeypatch.setattr(post_update.config, "settings", _AutoRunSettings(None))
    monkeypatch.setattr(post_update.utils, "should_offer_mq_start", lambda running=None: True)

    calls = {"restarted": [], "started": [], "eq_pids": [], "persisted": [], "loadouts": [], "commands": []}
    monkeypatch.setattr(
        post_update.config, "update_setting",
        lambda keys, value, **k: calls["persisted"].append(value),
    )
    monkeypatch.setattr(post_update.processes, "get_eqgame_process_pids", lambda: calls["eq_pids"])
    monkeypatch.setattr(
        post_update.processes, "restart_macroquest", lambda folder: calls["restarted"].append(folder)
    )
    monkeypatch.setattr(
        post_update.processes, "run_executable",
        lambda folder, exe: calls["started"].append((folder, exe)) or True,
    )
    monkeypatch.setattr(post_update.processes, "running_executable_paths", lambda: FRESH)
    monkeypatch.setattr(
        post_update.processes, "run_command",
        lambda command, cwd=None: calls["commands"].append((command, cwd)) or True,
    )
    monkeypatch.setattr(
        post_update.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: calls["loadouts"].append(running) or ([], []),
    )
    return monkeypatch, calls


def _execute(surface, decision, running=SNAPSHOT, mq_folder="C:\\VanillaMQ"):
    pending = post_update.PendingOffer(decision, running, mq_folder)
    asyncio.run(post_update.execute(pending, surface))


def test_execute_none_does_nothing(exec_env):
    monkeypatch, calls = exec_env
    surface = FakeSurface()
    _execute(surface, post_update.Decision.NONE)
    assert surface.asked_restart == 0 and surface.asked_cold == 0 and calls["loadouts"] == []


def test_execute_missing_folder_notifies_and_stops(exec_env):
    surface = FakeSurface()
    _execute(surface, post_update.Decision.RESTART, mq_folder=None)
    assert any("path not found" in msg and error for msg, error in surface.notices)
    assert surface.asked_restart == 0


def test_execute_restart_uses_fresh_rescan_for_loadout(exec_env):
    monkeypatch, calls = exec_env
    surface = FakeSurface()
    _execute(surface, post_update.Decision.RESTART)
    assert calls["restarted"] == ["C:\\VanillaMQ"]
    assert surface.waited_eq == 0            # EQ wasn't running
    assert calls["loadouts"] == [FRESH]      # not the stale snapshot


@pytest.mark.parametrize("auto_run", [True, False])
def test_execute_restart_always_asks_regardless_of_auto_run(exec_env, auto_run):
    # Hard constraint: AUTO_RUN_VVMQ auto-consents only to cold starts. A restart of a
    # running session always prompts, and False doesn't suppress the offer either.
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.config, "settings", _AutoRunSettings(auto_run))
    surface = FakeSurface()
    _execute(surface, post_update.Decision.RESTART)
    assert surface.asked_restart == 1 and surface.asked_cold == 0
    assert calls["restarted"] == ["C:\\VanillaMQ"]


def test_execute_restart_declined_notifies_and_stops(exec_env):
    monkeypatch, calls = exec_env
    surface = FakeSurface(restart_ok=False)
    _execute(surface, post_update.Decision.RESTART)
    assert calls["restarted"] == [] and calls["loadouts"] == []
    assert any("next time MacroQuest starts" in msg for msg, _ in surface.notices)


def test_execute_restart_waits_for_eq_then_proceeds(exec_env):
    monkeypatch, calls = exec_env
    calls["eq_pids"].append(200)
    surface = FakeSurface()
    _execute(surface, post_update.Decision.RESTART)
    assert surface.waited_eq == 1
    assert calls["restarted"] == ["C:\\VanillaMQ"]


def test_execute_eq_close_cancel_skips_restart(exec_env):
    monkeypatch, calls = exec_env
    calls["eq_pids"].append(200)
    surface = FakeSurface(eq_close_ok=False)
    _execute(surface, post_update.Decision.RESTART)
    assert calls["restarted"] == [] and calls["loadouts"] == []
    assert any("Restart skipped" in msg for msg, _ in surface.notices)


def test_execute_restart_failure_notifies_and_skips_loadout(exec_env):
    monkeypatch, calls = exec_env

    def _boom(folder):
        raise RuntimeError("still running")

    monkeypatch.setattr(post_update.processes, "restart_macroquest", _boom)
    surface = FakeSurface()
    _execute(surface, post_update.Decision.RESTART)
    assert calls["loadouts"] == []
    assert any("already applied" in msg and error for msg, error in surface.notices)


def test_execute_cold_start_aborts_when_mq_appeared_during_prompt(exec_env):
    # The user started MQ themselves while the prompt sat open: launching a second
    # loader would trip MQ's blocking alternate-loader dialog.
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.utils, "should_offer_mq_start", lambda running=None: False)
    surface = FakeSurface(cold_choice="yes")
    _execute(surface, post_update.Decision.COLD_START)
    assert calls["started"] == [] and calls["loadouts"] == []
    assert any("already running" in msg for msg, _ in surface.notices)


def test_execute_cold_start_auto_true_skips_prompt(exec_env):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.config, "settings", _AutoRunSettings(True))
    surface = FakeSurface()
    _execute(surface, post_update.Decision.COLD_START)
    assert surface.asked_cold == 0
    assert calls["started"] == [("C:\\VanillaMQ", "MacroQuest.exe")]
    assert calls["loadouts"] == [FRESH]


def test_execute_cold_start_auto_false_is_silent_noop(exec_env):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.config, "settings", _AutoRunSettings(False))
    surface = FakeSurface()
    _execute(surface, post_update.Decision.COLD_START)
    assert surface.asked_cold == 0 and calls["started"] == []
    assert calls["loadouts"] == [] and surface.notices == []


def test_execute_cold_start_failure_notifies_and_skips_loadout(exec_env):
    monkeypatch, calls = exec_env

    def _boom(folder, exe):
        raise FileNotFoundError("MacroQuest.exe not found")

    monkeypatch.setattr(post_update.processes, "run_executable", _boom)
    surface = FakeSurface(cold_choice="yes")
    _execute(surface, post_update.Decision.COLD_START)
    assert calls["loadouts"] == []
    assert any("Failed to start MacroQuest" in msg and error for msg, error in surface.notices)


@pytest.mark.parametrize(
    "choice, starts, persisted",
    [("yes", True, []), ("no", False, []), ("always", True, [True]), ("never", False, [False])],
)
def test_execute_cold_start_choice_matrix(exec_env, choice, starts, persisted):
    monkeypatch, calls = exec_env
    surface = FakeSurface(cold_choice=choice)
    _execute(surface, post_update.Decision.COLD_START)
    assert (calls["started"] != []) is starts
    assert calls["persisted"] == persisted   # policy owns the config write
    assert surface.ui_synced == persisted    # surface only syncs its widgets
    assert (calls["loadouts"] != []) is starts


def test_execute_loadout_runs_commands_and_notifies_skips(exec_env):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(
        post_update.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: ([(["C:\\x\\EQBCS.exe"], "C:\\x")], ["C:\\y\\MySEQ.exe"]),
    )
    surface = FakeSurface(cold_choice="yes")
    _execute(surface, post_update.Decision.COLD_START)
    assert calls["commands"] == [(["C:\\x\\EQBCS.exe"], "C:\\x")]
    assert any("MySEQ.exe is already running" in msg for msg, _ in surface.notices)


def test_execute_loadout_failure_notifies_and_continues(exec_env):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(
        post_update.utils, "resolve_post_update_launch_filtered",
        lambda env=None, running=None: (
            [("missing-tool --flag", None), (["C:\\x\\EQBCS.exe"], "C:\\x")], []
        ),
    )

    def _run(command, cwd=None):
        if command == "missing-tool --flag":
            raise FileNotFoundError("missing-tool")
        calls["commands"].append((command, cwd))
        return True

    monkeypatch.setattr(post_update.processes, "run_command", _run)
    surface = FakeSurface(cold_choice="yes")
    _execute(surface, post_update.Decision.COLD_START)
    assert calls["commands"] == [(["C:\\x\\EQBCS.exe"], "C:\\x")]  # failure didn't stop the batch
    assert any("Failed to start missing-tool" in msg and error for msg, error in surface.notices)


# --- offer/prepare: the scan -> decide -> execute wiring -------------------------------

@pytest.mark.parametrize(
    "mq_running, expected",
    [(True, post_update.Decision.RESTART), (False, post_update.Decision.COLD_START)],
)
def test_offer_wires_scan_decision_and_folder(exec_env, mq_running, expected):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.processes, "running_executable_paths", lambda: SNAPSHOT)
    monkeypatch.setattr(post_update.utils, "macroquest_running", lambda running=None: mq_running)
    monkeypatch.setattr(post_update.utils, "get_vvmq_path", lambda: "C:\\VanillaMQ")
    captured = {}

    async def _execute_spy(pending, surface):
        captured["pending"] = pending

    monkeypatch.setattr(post_update, "execute", _execute_spy)

    outcome = SyncOutcome(success=True, vvmq_updated=True)
    asyncio.run(post_update.offer(outcome, FakeSurface()))

    assert captured["pending"].decision is expected
    assert captured["pending"].running is SNAPSHOT   # the pre-prompt snapshot is what execute gets
    assert captured["pending"].mq_folder == "C:\\VanillaMQ"


def _scan_boom():
    raise AssertionError("the process scan must not run on a short-circuited prepare")


def test_prepare_non_windows_short_circuits(exec_env):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.sys, "platform", "linux")
    monkeypatch.setattr(post_update.processes, "running_executable_paths", _scan_boom)
    pending = asyncio.run(post_update.prepare(SyncOutcome(success=True, vvmq_updated=True)))
    assert pending.decision is post_update.Decision.NONE
    assert pending.running is None and pending.mq_folder is None


def test_prepare_ci_short_circuits(exec_env):
    monkeypatch, calls = exec_env
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(post_update.processes, "running_executable_paths", _scan_boom)
    pending = asyncio.run(post_update.prepare(SyncOutcome(success=True, vvmq_updated=True)))
    assert pending.decision is post_update.Decision.NONE


@pytest.mark.parametrize(
    "outcome",
    [
        SyncOutcome(success=True, vvmq_updated=False),   # the common no-op sync
        SyncOutcome(success=False),                      # fully failed run
    ],
)
def test_prepare_skips_scan_when_no_vvmq_write(exec_env, outcome):
    monkeypatch, calls = exec_env
    monkeypatch.setattr(post_update.processes, "running_executable_paths", _scan_boom)
    pending = asyncio.run(post_update.prepare(outcome))
    assert pending.decision is post_update.Decision.NONE
    assert pending.running is None and pending.mq_folder is None


# --- CLI wiring: handle_download_watched_async hands off to post_update.offer ----------

def _run_watched(monkeypatch, outcome):
    offered = []

    async def _no(*a, **k):
        return False

    async def _run_sync(*a, **k):
        return outcome

    async def _offer(outcome_arg, surface):
        offered.append((outcome_arg, surface))

    monkeypatch.setattr(main.net, "is_mq_down", _no)
    monkeypatch.setattr(main, "prompt_navmesh_opt_in", lambda: None)
    monkeypatch.setattr(main.sync, "run_sync", _run_sync)
    monkeypatch.setattr(main.post_update, "offer", _offer)
    result = asyncio.run(main.handle_download_watched_async("db", {}))
    return result, offered


def test_watched_flow_offers_post_update_on_success(monkeypatch):
    result, offered = _run_watched(monkeypatch, SyncOutcome(success=True, vvmq_updated=True))
    assert result is True
    assert len(offered) == 1
    assert isinstance(offered[0][1], main._CliPostUpdate)


def test_watched_flow_delegates_offer_on_partial_failure(monkeypatch):
    # Gating moved into prepare(): the CLI delegates unconditionally, so a run that
    # updated VVMQ but had an unrelated failure still reaches the offer, and result
    # keeps reporting the failure for the exit status.
    result, offered = _run_watched(monkeypatch, SyncOutcome(success=False, vvmq_updated=True))
    assert result is False
    assert len(offered) == 1


# --- TUI surface adapter: modal responses -> the protocol's vocabulary ------------------

class _StubApp:
    def __init__(self, response=None):
        self._response = response

    async def push_screen_wait(self, screen):
        return self._response


@pytest.fixture
def tui_surface_cls():
    # terminal_ui reads config.settings at import; stub it if tests run unconfigured
    from redfetch import config

    if config.settings is None:
        config.settings = _AutoRunSettings(None)
    from redfetch.terminal_ui import _TuiPostUpdate

    return _TuiPostUpdate


@pytest.mark.parametrize(
    "response, expected",
    [("run", "yes"), ("always", "always"), ("never", "never"), ("skip", "no")],
)
def test_tui_ask_cold_start_maps_modal_responses(tui_surface_cls, response, expected):
    surface = tui_surface_cls(_StubApp(response))
    assert asyncio.run(surface.ask_cold_start()) == expected
