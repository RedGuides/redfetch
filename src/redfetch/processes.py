"""Windows-specific helpers for managing external executables."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Sequence, Tuple

import psutil

IS_WINDOWS = sys.platform == "win32"


if IS_WINDOWS:
    from .unloadmq import force_remote_unload
else:  # skip if not on windows
    def force_remote_unload() -> None:
        pass


def _normalized_executables(folder_path: str) -> List[str]:
    folder = os.path.normpath(os.path.abspath(folder_path))
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
        print("Running executables is only supported on Windows.")
        return False

    if not folder_path:
        print(f"Folder path not set for {executable_name}")
        return False

    executable_path = os.path.join(folder_path, executable_name)
    if not os.path.isfile(executable_path):
        print(f"{executable_name} not found in the specified folder.")
        return False

    try:
        subprocess.Popen([executable_path, *(args or [])], cwd=folder_path)
        print(f"{executable_name} started successfully.")
        return True
    except Exception as exc:
        print(f"Failed to start {executable_name}: {exc}")
        return False

