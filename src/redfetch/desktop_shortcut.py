"""
Windows desktop shortcut management for redfetch.

This is intentionally Windows-only. The public API is safe to import on any OS,
but will raise NotImplementedError for non-Windows platforms when called.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SHORTCUT_FILENAME = "redfetch.lnk"


def get_shortcut_path() -> Path:
    """Absolute path to the redfetch desktop shortcut."""
    if sys.platform != "win32":
        raise NotImplementedError("Desktop shortcuts are only supported on Windows.")

    from win32com.shell import shell, shellcon  # type: ignore

    # CSIDL_DESKTOPDIRECTORY is the per-user Desktop path (handles redirection).
    desktop_dir = Path(
        shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0)
    )
    return desktop_dir / SHORTCUT_FILENAME


def create_shortcut(overwrite: bool = True) -> Path:
    """Create (or overwrite) the desktop shortcut."""
    if sys.platform != "win32":
        raise NotImplementedError("Desktop shortcuts are only supported on Windows.")

    shortcut_path = get_shortcut_path()
    if shortcut_path.exists() and not overwrite:
        return shortcut_path

    pyapp = os.environ.get("PYAPP")
    if pyapp and Path(pyapp).exists():
        target = Path(pyapp)
        args = ""
    else:
        cmd = shutil.which("redfetch")
        if cmd:
            target = Path(cmd)
            args = ""
        else:
            target = Path(sys.executable)
            args = "-m redfetch.main"

    icon_candidates = [
        target.with_suffix(".ico"),
        target.parent / "redfetch.ico",
        Path.cwd() / "redfetch.ico",
        Path(__file__).resolve().parent / "redfetch.ico",
    ]
    icon_location = next(
        (f"{p},0" for p in icon_candidates if p.exists()),
        f"{target},0",
    )

    from win32com.client import Dispatch  # type: ignore

    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(shortcut_path))
    sc.TargetPath = str(target)
    sc.Arguments = args
    sc.WorkingDirectory = str(Path.home())
    sc.Description = "redfetch"
    sc.IconLocation = icon_location
    sc.Save()
    return shortcut_path


def remove_shortcut() -> Path:
    """Remove the desktop shortcut (no-op if missing)."""
    if sys.platform != "win32":
        raise NotImplementedError("Desktop shortcuts are only supported on Windows.")
    shortcut_path = get_shortcut_path()
    try:
        shortcut_path.unlink()
    except FileNotFoundError:
        pass
    return shortcut_path

