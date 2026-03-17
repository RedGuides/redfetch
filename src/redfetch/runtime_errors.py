"""Shared fatal error helpers for startup/CLI boundaries."""

from __future__ import annotations

import os
import sys
import traceback
from typing import NoReturn


def is_windows_pyapp() -> bool:
    """are we using the redfetch.exe executable on windows?"""
    return sys.platform == "win32" and bool(os.getenv("PYAPP"))

def normalize_error_message(
    message: str | BaseException | None,
    *,
    fallback: str = "An unexpected error occurred.",
) -> str:
    """Make a safe non-empty string."""
    if isinstance(message, BaseException):
        normalized = str(message).strip()
        if normalized:
            return normalized
        return f"{message.__class__.__name__} ({fallback})"

    if message is None:
        return fallback

    normalized = str(message).strip()
    if normalized:
        return normalized
    return fallback


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
    summary = f"{error.__class__.__name__}: {normalize_error_message(error)}"
    traceback_text = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    if not traceback_text:
        return summary
    return f"{summary}\n\n{traceback_text}"


def display_fatal_error(
    message: str | BaseException | None,
) -> str:
    """Print a fatal error and optionally show a Windows MessageBox."""
    normalized_message = normalize_error_message(message)
    error_details = (
        _format_error_details(message)
        if isinstance(message, BaseException)
        else None
    )

    if is_windows_pyapp():
        if error_details:
            dialog_message = (
                "Tip: Press Ctrl+C to copy this error report.\n\n"
                f"{error_details}"
            )
            print(error_details, file=sys.stderr)
        else:
            dialog_message = normalized_message
            print(normalized_message, file=sys.stderr)
        _show_windows_error_dialog(dialog_message)
    else:
        # CLI behavior: print the raw normalized message without extra wrappers.
        if error_details:
            print(error_details, file=sys.stderr)
        else:
            print(normalized_message, file=sys.stderr)

    return normalized_message


def exit_with_fatal_error(
    message: str | BaseException | None,
    exit_code: int = 1,
) -> NoReturn:
    """Display a fatal error and terminate with a non-zero exit code."""
    display_fatal_error(message)
    raise SystemExit(exit_code)
