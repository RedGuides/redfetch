"""Helpers for managing external processes."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import psutil

IS_WINDOWS = sys.platform == "win32"


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
                normalized = os.path.normcase(os.path.normpath(exe_path))
                if normalized in exec_paths:
                    print(f"Process '{exe_path}' (PID {proc.pid}) is currently running.")
                    running.append((proc.pid, exe_path))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return running
    except Exception as exc:
        print(f"An error occurred while checking running processes: {exc}")
        return running


def find_processes_locking_dirs(
    folder_path: str, target_dirs: Iterable[str]
) -> List[Tuple[int, str]]:
    """Return processes whose mapped modules live under any of ``target_dirs`` (those files
    can't be overwritten while the process is running).
    """
    if not IS_WINDOWS:
        return []

    normalized_targets = [
        os.path.normcase(os.path.normpath(d)) for d in target_dirs if d
    ]
    if not normalized_targets:
        return []

    candidates: dict[int, str] = {}
    for pid, exe_path in are_executables_running_in_folder(folder_path):
        candidates[pid] = exe_path
    for pid in get_eqgame_process_pids():
        if pid in candidates:
            continue
        try:
            candidates[pid] = psutil.Process(pid).exe() or "eqgame.exe"
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            candidates[pid] = "eqgame.exe"

    def _maps_under_target(pid: int) -> bool:
        try:
            for mmap in psutil.Process(pid).memory_maps():
                path = getattr(mmap, "path", None)
                if not path:
                    continue
                normalized = Path(os.path.normcase(os.path.normpath(path)))
                if any(normalized.is_relative_to(target) for target in normalized_targets):
                    return True
            return False
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return True  # can't see the maps -> assume collision to stay safe
        except Exception as exc:
            print(f"Could not inspect modules for PID {pid}: {exc}")
            return True

    colliding: List[Tuple[int, str]] = []
    for pid, exe_path in candidates.items():
        if _maps_under_target(pid):
            colliding.append((pid, exe_path))
    return colliding


def terminate_executables_in_folder(folder_path: str) -> None:
    """Terminate running executables inside ``folder_path`` and unload MacroQuest."""
    if not IS_WINDOWS:
        print("Terminating executables is only supported on Windows platforms.")
        return

    running = are_executables_running_in_folder(folder_path)
    for pid, exe_path in running:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=5)
            print(f"Terminated process '{exe_path}' (PID {pid}).")
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, psutil.ZombieProcess) as err:
            print(f"Could not terminate process: {err}")

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

