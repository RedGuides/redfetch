"""PyApp entry point for redfetch.exe: run the app, and self-heal a broken install.

PyApp treats half-installed environments as complete, so if something fails or 
interrupts install, it stays broken unless someone opens a terminal and runs `self restore`. 
This wrapper runs `self restore` automatically.

Embedded via PYAPP_EXEC_SCRIPT. Stdlib-only, and must not import redfetch at module
scope, since redfetch is exactly what may be missing!
"""

import sys


def _import_entrypoint():
    from redfetch.main import main
    return main


# Repair runs after we exit, in a detached script: on Windows `self restore` can't
# delete the install dir while our python.exe is still running from inside it.
_REPAIR_BAT = r"""@echo off
title redfetch - finishing setup
echo.
echo   redfetch setup didn't finish!
echo   Trying to finish it now, this may take a few minutes.
echo   Please don't close this window.
echo.
timeout /t 3 /nobreak > nul
"<EXE>" self restore
if %errorlevel% neq 0 goto redfetch_failed
echo.
echo   All set -- starting redfetch...
set "REDFETCH_SELFHEAL=1"
"<EXE>"
goto redfetch_cleanup
:redfetch_failed
echo.
echo   Automatic repair didn't complete. You can repair manually by running:
echo       "<EXE>" self restore
echo.
pause
:redfetch_cleanup
(goto) 2>nul & del "%~f0"
"""


def _schedule_repair() -> "int | None":
    """Schedule `self restore` + relaunch; return 0 if scheduled, else None."""
    import os

    # PYAPP is always set (to "1" when PASS_LOCATION is off), so require a real path.
    exe = os.environ.get("PYAPP")
    if sys.platform != "win32" or not exe or not os.path.exists(exe):
        return None
    if os.environ.get("REDFETCH_SELFHEAL"):  # already tried once this chain
        return None

    import subprocess
    import tempfile

    script = _REPAIR_BAT.replace("<EXE>", exe.replace("%", "%%"))  # %% so cmd won't expand the path
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="redfetch-repair-", suffix=".bat")
        with os.fdopen(fd, "w") as handle:
            handle.write(script)
        subprocess.Popen(
            ["cmd.exe", "/c", path],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except Exception:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        return None
    return 0


def main() -> int:
    try:
        entrypoint = _import_entrypoint()
    except ImportError:
        code = _schedule_repair()
        if code is None:
            raise
        print("redfetch needs to finish setting up; opening a repair window...",
              file=sys.stderr)
        return code

    sys.argv[0] = "redfetch"  # so typer's --help shows "redfetch", not this script's path
    entrypoint()  # errors past here are app logic, not install problems -> don't self-heal
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
