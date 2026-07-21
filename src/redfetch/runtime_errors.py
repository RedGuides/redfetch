"""Shared fatal error helpers for startup/CLI boundaries."""

from __future__ import annotations

import os
import sys
import traceback
from typing import NoReturn


def is_windows_pyapp() -> bool:
    """are we using the redfetch.exe executable on windows?"""
    return sys.platform == "win32" and bool(os.getenv("PYAPP"))


def _show_windows_error_dialog(message: str) -> None:
    """Shows a Windows MessageBox."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "redfetch", 0x10)
    except Exception:
        # Best effort only: CLI stderr output remains the source of truth.
        pass


def _format_error_details(error: BaseException) -> str:
    """Summarize the exception with traceback and details."""
    message = str(error).strip() or error.__class__.__name__
    summary = f"{error.__class__.__name__}: {message}"
    traceback_text = "".join(traceback.format_exception(error)).strip()
    if not traceback_text:
        return summary
    return f"{summary}\n\n{traceback_text}"


def display_fatal_error(error: BaseException) -> None:
    """Print a fatal error and optionally show a Windows MessageBox."""
    if not isinstance(error, BaseException):  # last-resort handler: never crash while crashing
        error = RuntimeError(str(error))

    error_details = _format_error_details(error)
    print(error_details, file=sys.stderr)
    if is_windows_pyapp():
        _show_windows_error_dialog(
            "Tip: Press Ctrl+C to copy this error report.\n\n"
            f"{error_details}"
        )


def exit_with_fatal_error(
    error: BaseException,
    exit_code: int = 1,
) -> NoReturn:
    """Display a fatal error and terminate with a non-zero exit code."""
    display_fatal_error(error)
    raise SystemExit(exit_code)
