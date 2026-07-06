"""Helpers for managing external processes."""
from __future__ import annotations

import configparser
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Sequence, Tuple

import psutil

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg
else:
    winreg = None

_PROC_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _normalized_executables(folder_path: str) -> List[str]:
    folder = os.path.normpath(os.path.abspath(folder_path))
    if not os.path.isdir(folder):
        return []
    return [
        os.path.normcase(os.path.join(folder, entry))
        for entry in os.listdir(folder)
        if entry.lower().endswith(".exe")
    ]


def are_executables_running_in_folder(folder_path: str) -> List[Tuple[int, str]]:
    """Return running executables located within ``folder_path``."""
    if not IS_WINDOWS:
        return []

    running: List[Tuple[int, str]] = []
    try:
        exec_paths = _normalized_executables(folder_path)
        if not exec_paths:
            return running

        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe_path = proc.info.get("exe")
                if not exe_path or not os.path.isfile(exe_path):
                    continue
                if _norm(exe_path) in exec_paths:
                    print(f"Process '{exe_path}' (PID {proc.pid}) is currently running.")
                    running.append((proc.pid, exe_path))
            except _PROC_GONE:
                continue
        return running
    except Exception as exc:
        print(f"An error occurred while checking running processes: {exc}")
        return running


def get_eqgame_process_pids() -> List[int]:
    """PIDs of running eqgame.exe processes. Observed only, never touched."""
    pids: List[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info.get("name")
            if name and name.lower() == "eqgame.exe":
                pids.append(proc.info["pid"])
        except _PROC_GONE:
            continue
    return pids


def running_executable_paths() -> set[str]:
    paths: set[str] = set()
    for proc in psutil.process_iter(["exe"]):
        try:
            exe_path = proc.info.get("exe")
            if exe_path:
                paths.add(_norm(exe_path))
        except _PROC_GONE:
            continue
    return paths


def is_executable_running(exe_path: str, running: set[str] | None = None) -> bool:
    if running is None:
        running = running_executable_paths()
    return _norm(exe_path) in running


def _spawned_loader_name(mq_folder: str) -> str | None:
    """Loader-copy filename from MacroQuest.ini."""
    for ini_path in (
        os.path.join(mq_folder, "config", "MacroQuest.ini"),
        os.path.join(mq_folder, "MacroQuest.ini"),
    ):

        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(ini_path, encoding="utf-8")
            name = parser.get("Internal", "SpawnedProcess", fallback="").strip()
        except (configparser.Error, OSError):
            continue
        if name:
            return name
    return None


def _spawned_loader_processes(mq_folder: str) -> List[Tuple[int, str]]:
    """Running processes matching the recorded loader-copy name."""
    spawned = _spawned_loader_name(mq_folder)
    if not spawned:
        return []
    spawned = spawned.lower()
    procs: List[Tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = proc.info.get("name")
            if name and name.lower() == spawned:
                procs.append((proc.info["pid"], proc.info.get("exe") or name))
        except _PROC_GONE:
            continue
    return procs


def macroquest_session_running(mq_folder: str, running: set[str] | None = None) -> bool:
    """True when an MQ session runs from *mq_folder*."""
    if running is None:
        running = running_executable_paths()
    folder = _norm(mq_folder)
    alias = _spawned_loader_name(mq_folder)
    alias = alias.lower() if alias else None
    for path in running:
        base = os.path.basename(path)
        if base == alias or (base == "macroquest.exe" and os.path.dirname(path) == folder):
            return True
    return False


_GRACEFUL_CLOSE_SEC = 3          # WM_CLOSE grace before forcing windowed apps (the loader)
_RESTART_LEFTOVER_GRACE_SEC = 2  # slow exits: re-check once before declaring a restart stuck

# A surviving EQBCS never trips MQ's "exit the alternate loader" dialog
_LEFTOVER_IGNORE = frozenset({"eqbcs.exe"})


def _post_wm_close(pids: set[int]) -> set[int]:
    """Ask each pid's top-level windows to close cleanly; return the pids we reached."""
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return set()

    posted: set[int] = set()

    def _visit(hwnd, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid in pids:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                posted.add(wpid)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_visit, None)
    except Exception:
        pass
    return posted


def _terminate_processes(procs: List[Tuple[int, str]]) -> None:
    """Terminate *procs* — WM_CLOSE first for a clean shutdown, then force; one shared wait."""
    names: dict[int, str] = {}
    targets = []
    for pid, exe_path in procs:
        try:
            targets.append(psutil.Process(pid))
            names[pid] = exe_path
        except psutil.NoSuchProcess:
            pass
    if not targets:
        return

    graceful = _post_wm_close(set(names))
    if graceful:
        _, unclosed = psutil.wait_procs(
            [p for p in targets if p.pid in graceful], timeout=_GRACEFUL_CLOSE_SEC
        )
        force = [p for p in targets if p.pid not in graceful] + list(unclosed)
    else:
        force = targets
    for proc in force:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, psutil.ZombieProcess) as err:
            print(f"Could not terminate process: {err}")

    gone, alive = psutil.wait_procs(targets, timeout=5)
    for proc in gone:
        print(f"Terminated process '{names[proc.pid]}' (PID {proc.pid}).")
    for proc in alive:  # the caller's re-scan decides what happens next
        print(f"Process '{names[proc.pid]}' (PID {proc.pid}) is taking a while to exit.")


def _excluding(procs: List[Tuple[int, str]], exclude: frozenset[str]) -> List[Tuple[int, str]]:
    return [(pid, path) for pid, path in procs if os.path.basename(path).lower() not in exclude]


def terminate_folder_processes(folder_path: str) -> None:
    """Close everything running from *folder_path* but never eqgame; the user closes EQ."""
    if not IS_WINDOWS:
        print("Terminating executables is only supported on Windows platforms.")
        return

    _terminate_processes(are_executables_running_in_folder(folder_path))


def _restart_leftovers(mq_folder: str) -> List[Tuple[int, str]]:
    """Processes that would block a clean relaunch: an in-folder loader copy or the
    RunFromTemp copy still alive."""
    return (
        _excluding(are_executables_running_in_folder(mq_folder), _LEFTOVER_IGNORE)
        + _spawned_loader_processes(mq_folder)
    )


def restart_macroquest(mq_folder: str) -> None:
    """Close the MQ folder's processes and relaunch. Caller has confirmed EQ is closed."""
    if not IS_WINDOWS:
        return
    # TOCTOU backstop: if EQ came back after the caller's gate, abort before touching the loader.
    if get_eqgame_process_pids():
        raise RuntimeError("EverQuest is still running; close it and restart MacroQuest")

    terminate_folder_processes(mq_folder)
    _terminate_processes(_spawned_loader_processes(mq_folder))  # RunFromTemp copy lives outside the folder

    leftovers = _restart_leftovers(mq_folder)
    if leftovers:
        time.sleep(_RESTART_LEFTOVER_GRACE_SEC)  # give slow exits one more chance
        leftovers = _restart_leftovers(mq_folder)
    if leftovers:
        names = ", ".join(sorted({os.path.basename(path) for _, path in leftovers}))
        raise RuntimeError(f"{names} could not be closed")
    run_executable(mq_folder, "MacroQuest.exe")


def run_executable(folder_path: str, executable_name: str, args: Sequence[str] | None = None) -> bool:
    """Launch ``executable_name`` located in ``folder_path`` with optional arguments."""
    if not IS_WINDOWS:
        raise RuntimeError("Running executables is only supported on Windows.")

    if not folder_path:
        raise ValueError(f"Folder path not set for {executable_name}")

    executable_path = os.path.join(folder_path, executable_name)
    if not os.path.isfile(executable_path):
        raise FileNotFoundError(f"{executable_name} not found in the specified folder.")

    subprocess.Popen([executable_path, *(args or [])], cwd=folder_path)
    print(f"{executable_name} started successfully.")
    return True


def run_command(command: "str | Sequence[str]", cwd: str | None = None) -> bool:
    """Launch a command that may be resolved through PATH."""
    if isinstance(command, str):
        if not command.strip():
            raise ValueError("No command to run.")
        popen_arg: "str | list[str]" = command
        display = command
    else:
        argv = list(command)
        if not argv:
            raise ValueError("No command to run.")
        popen_arg = argv
        display = subprocess.list2cmdline(argv)

    subprocess.Popen(popen_arg, cwd=cwd)
    print(f"Started: {display}")
    return True


def open_folder(path: str) -> None:
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Directory does not exist: {path}")
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_file(folder: str, filename: str) -> str:
    """Open a file and return a short descriptor for fallback handlers."""
    full_path = os.path.join(folder, filename)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"File not found: {full_path}")

    if IS_WINDOWS:
        file_ext = os.path.splitext(filename)[1].lower()
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, file_ext) as key:
                winreg.QueryValue(key, "")
                os.startfile(full_path)  # type: ignore[attr-defined]
            return "with its default program"
        except OSError:
            subprocess.Popen(["notepad.exe", full_path])
            return "with Notepad"

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", full_path])
        else:
            subprocess.Popen(["xdg-open", full_path])
        return ""
    except Exception:
        webbrowser.open(Path(full_path).as_uri())
        return "in your browser"

