"""Helpers for managing external processes."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import psutil

IS_WINDOWS = sys.platform == "win32"

# Common psutil failures while scanning processes.
_PROC_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


if IS_WINDOWS:
    from .unloadmq import force_remote_unload, get_eqgame_process_pids
else:  # skip if not on windows
    def force_remote_unload() -> None:
        pass

    def get_eqgame_process_pids() -> List[int]:
        return []


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


def _is_under_any(path: str, targets: Sequence[str]) -> bool:
    normalized = Path(_norm(path))
    return any(normalized.is_relative_to(target) for target in targets)


def _has_module_under(pid: int, targets: Sequence[str]) -> bool:
    try:
        return any(
            _is_under_any(p, targets)
            for m in psutil.Process(pid).memory_maps()
            if (p := getattr(m, "path", None))
        )
    except _PROC_GONE:
        return True
    except Exception as exc:
        # Access failures are safer to treat as locks; otherwise the overwrite can fail later.
        print(f"Could not inspect modules for PID {pid}: {exc}")
        return True


def _mq_module_holders(folder_path: str) -> dict[int, str]:
    holders: dict[int, str] = dict(are_executables_running_in_folder(folder_path))
    # eqgame lives outside MQ but can hold injected MQ modules open.
    for pid in get_eqgame_process_pids():
        if pid not in holders:
            try:
                holders[pid] = psutil.Process(pid).exe() or "eqgame.exe"
            except _PROC_GONE:
                holders[pid] = "eqgame.exe"
    return holders


def find_processes_locking_dirs(
    folder_path: str, target_dirs: Iterable[str]
) -> List[Tuple[int, str]]:
    if not IS_WINDOWS:
        return []

    targets = [_norm(d) for d in target_dirs if d]
    if not targets:
        return []

    # (1) MQ/eqgame processes with a target-dir module mapped.
    colliding: dict[int, str] = {
        pid: exe
        for pid, exe in _mq_module_holders(folder_path).items()
        if _has_module_under(pid, targets)
    }

    # (2) Any process whose own executable sits under a target dir.
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            exe_path = proc.info.get("exe")
            if exe_path and proc.pid not in colliding and _is_under_any(exe_path, targets):
                colliding[proc.pid] = exe_path
        except _PROC_GONE:
            continue

    return list(colliding.items())


def terminate_processes(
    processes_to_close: Sequence[Tuple[int, str]], mq_folder: str | None = None
) -> None:
    """Close colliders; if MQ is involved, close the MQ folder and unload eqgame."""
    if not IS_WINDOWS:
        print("Terminating executables is only supported on Windows platforms.")
        return

    eqgame_pids = set(get_eqgame_process_pids())
    mq_folder_exes = dict(are_executables_running_in_folder(mq_folder)) if mq_folder else {}
    mq_involved = any(
        pid in eqgame_pids or pid in mq_folder_exes for pid, _ in processes_to_close
    )

    to_close = dict(processes_to_close)
    if mq_involved:
        to_close.update(mq_folder_exes)

    for pid, exe_path in to_close.items():
        if pid in eqgame_pids:
            continue  # unload MacroQuest from the game, but do not kill the game
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=5)
            print(f"Terminated process '{exe_path}' (PID {pid}).")
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, psutil.ZombieProcess) as err:
            print(f"Could not terminate process: {err}")

    if mq_involved:
        try:
            force_remote_unload()
        except Exception as exc:
            print(f"Error unloading MacroQuest: {exc}")


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

