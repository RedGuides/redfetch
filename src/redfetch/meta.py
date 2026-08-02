"""Version checking, self-update, and uninstall."""

# Standard
import os
import platform
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import NoReturn

# Third-party
import httpx
from packaging import version

# Rich library
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Confirm

# Local
from redfetch.__about__ import __version__
from redfetch import cache
from redfetch import config


def _get_pypi_url() -> str:
    """Pick PyPI JSON URL, favouring `REDFETCH_PYPI_URL` if set."""
    env_url = os.getenv("REDFETCH_PYPI_URL")
    if env_url:
        return env_url
    if version.parse(__version__).is_devrelease:
        return "https://test.pypi.org/pypi/redfetch/json"
    return "https://pypi.org/pypi/redfetch/json"


PYPI_URL = _get_pypi_url()

console = Console()


def get_current_version():
    return __version__


_UPDATE_CACHE_TTL_SECONDS = 2 * 60 * 60  # 2 hours


def clear_pypi_cache() -> None:
    """Clear cached PyPI metadata."""
    cache.shared().delete(f"pypi_latest:{PYPI_URL}")


def fetch_latest_version_cached():
    """Fetch latest PyPI version with a 2-hour disk-backed cache."""
    disk_cache = cache.shared()
    cache_key = f"pypi_latest:{PYPI_URL}"
    cached = disk_cache.get(cache_key)
    if cached is not None:
        return cached
    latest = fetch_latest_version_from_pypi()
    disk_cache.set(cache_key, latest, expire=_UPDATE_CACHE_TTL_SECONDS)
    return latest


def fetch_latest_version_from_pypi():
    response = httpx.get(PYPI_URL, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    # On TestPyPI, prefer the highest available release (including pre-releases)
    if "test.pypi.org" in PYPI_URL:
        releases = list(data.get("releases", {}).keys())
        if releases:
            return max(releases, key=version.parse)
    # Default: whatever PyPI reports as the latest stable version
    return data["info"]["version"]


def detect_installation_method():
    """Detect how the package was installed."""
    try:
        # Check for PYAPP first
        if os.getenv('PYAPP'):
            return 'pyapp'

        # Get the package location
        package_location = Path(__file__).parent.absolute()

        location_str = str(package_location)
        parts_lower = {part.lower() for part in package_location.parts}

        # Check for pipx
        if 'pipx' in location_str:
            return 'pipx'

        # uv paths contain ".../uv/.../tools/..."
        if 'uv' in parts_lower and 'tools' in parts_lower:
            return 'uv'

        # Default to pip
        return 'pip'
    except Exception:
        return 'pip'


def get_update_command():
    """Update command for the installation method, or None."""
    if "test.pypi.org" in PYPI_URL:
        return None

    method = detect_installation_method()
    commands = {
        'pip': [sys.executable, '-m', 'pip', 'install', '--upgrade', 'redfetch'],
        'pipx': ['pipx', 'upgrade', 'redfetch'],
        'uv': ['uv', 'tool', 'upgrade', 'redfetch'],
    }
    return commands.get(method)


def _sweep_pip_stash_debris():
    # leftover pip uninstall stashes (~edfetch, ~-dfetch, ...) from interrupted upgrades
    site_packages = Path(__file__).resolve().parent.parent
    if site_packages.name != 'site-packages':
        return
    for entry in site_packages.iterdir():
        if not (entry.is_dir() and entry.name.startswith('~') and len(entry.name) == len('redfetch')):
            continue
        tail = entry.name.lstrip('-~.=%0123456789')
        if tail and 'redfetch'.endswith(tail):
            shutil.rmtree(entry, ignore_errors=True)


def check_for_update():
    current_version = get_current_version()
    
    try:
        latest_version = fetch_latest_version_cached()
        
        if version.parse(latest_version) > version.parse(current_version):
            version_info = Panel(
                Text.assemble(
                    ("An update for redfetch is available! 🚡\n\n", "bold green"),
                    ("Local version: ", "dim"),
                    (f"{current_version}\n", "cyan"),
                    ("Latest version: ", "dim"),
                    (f"{latest_version}", "cyan bold")
                ),
                title="Update Available",
                expand=False
            )
            console.print(version_info)
            
            # Handle PYAPP separately
            if os.getenv('PYAPP'):
                if Confirm.ask("Would you like to update now?"):
                    return self_update()
                else:
                    console.print("[yellow]Update skipped. You can manually update later.[/yellow]")
                return False
            
            # Get the appropriate update command
            update_command = get_update_command()
            if not update_command:
                if "test.pypi.org" in PYPI_URL:
                    console.print("[yellow]Dev builds don't auto-update: TestPyPI is only safe for redfetch itself. Install it with --index-url https://test.pypi.org/simple/ --no-deps, then install dependencies from PyPI.[/yellow]")
                else:
                    console.print("[red]Could not determine update method.[/red]")
                return False

            command_panel = Panel(
                Text(subprocess.list2cmdline(update_command), style="bold cyan"),
                title="Update Command",
                expand=False
            )
            console.print(command_panel)
            
            if Confirm.ask("Would you like to run this command to update?"):
                return pip_update_redfetch(update_command, latest_version)
            else:
                console.print("[yellow]Update skipped. You can manually update later.[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Error checking for updates:[/bold red] {e}")
    return False


def pip_update_redfetch(update_command, latest_version):
    manual_command = subprocess.list2cmdline(update_command)
    try:
        console.print(f"\n[bold]Updating redfetch to version {latest_version}...[/bold]\n")

        _sweep_pip_stash_debris()

        # Run the update command and let it print directly to console
        returncode = subprocess.run(update_command).returncode

        if returncode == 0:
            console.print("\n[bold green]redfetch has been successfully updated. 🫎[/bold green]")
            console.print("[yellow]Please run redfetch again to use the updated version.[/yellow]")
            sys.exit(0)
        else:
            console.print("\n[bold red]Update failed. See output above for details.[/bold red]")
            console.print(f"[yellow]You can update manually by closing redfetch and running:[/yellow] {manual_command}")
            sys.exit(1)
    except FileNotFoundError:
        console.print(f"\n[bold red]Couldn't find {update_command[0]} on PATH.[/bold red]")
        console.print(f"[yellow]You can update manually by closing redfetch and running:[/yellow] {manual_command}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error during update process:[/bold red] {e}")
        console.print(f"[yellow]You can update manually by closing redfetch and running:[/yellow] {manual_command}")
        sys.exit(1)


def spawn_silent_self_update() -> bool:
    try:
        if "test.pypi.org" in PYPI_URL:
            return False  # no updates for dev builds

        latest_version = fetch_latest_version_cached()
        if version.parse(latest_version) <= version.parse(get_current_version()):
            return False

        pyapp_exe = os.getenv('PYAPP')
        if pyapp_exe:
            update_command = [pyapp_exe, 'self', 'update']
        else:
            update_command = get_update_command()
            if not update_command:
                return False
            _sweep_pip_stash_debris()

        # CREATE_NO_WINDOW because a hidden run must not flash a console.
        subprocess.Popen(
            update_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return True
    except Exception:
        return False


def self_update() -> NoReturn:
    """Update with PYAPP."""
    try:
        console.print("[bold]Performing self-update...[/bold]")

        current_version = get_current_version()
        latest_version = fetch_latest_version_from_pypi()
        console.print(f"Current version: {current_version}")
        console.print(f"Latest version: {latest_version}")

        executable_path = os.getenv('PYAPP')
        update_command = [executable_path, 'self', 'update']

        # Start the update process in a new console and exit the current one
        subprocess.Popen(
            update_command,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )

        # Exit the current process to allow the update to proceed
        sys.exit(0)

    except Exception as e:
        console.print(f"[bold red]Error during self-update process:[/bold red] {e}")
        sys.exit(1)


def self_remove() -> None:
    """Remove with PYAPP."""
    try:
        console.print("[bold]Performing self-uninstall...[/bold]")

        executable_path = os.getenv('PYAPP')
        if not executable_path:
            console.print("[bold red]Executable path not found. Exiting self-remove.[/bold red]")
            return

        batch_file_path = Path(executable_path).with_name("uninstall.bat")
        # Match the UTF-8 code page selected by the script.
        batch_file_path.write_text(
            _uninstall_batch_script(executable_path, os.getpid()), encoding="utf-8"
        )

        # start's first quoted argument is the window title.
        subprocess.Popen(
            ['cmd.exe', '/c', 'start', '', batch_file_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )

        sys.exit(0)

    except Exception as e:
        console.print(f"[bold red]Error during self-uninstall process:[/bold red] {e}")
        input("Press Enter to close this window...")
        sys.exit(1)


def _uninstall_batch_script(executable_path: str, parent_pid: int) -> str:
    exe = executable_path.replace("%", "%%")  # Escape batch variable expansion.
    return textwrap.dedent(f"""
        @echo off
        chcp 65001 > nul
        echo Waiting for redfetch to close...

        set /a waited=0
        :wait_for_parent
        tasklist /fi "PID eq {parent_pid}" 2>nul | find "{parent_pid}" > nul || goto parent_gone
        set /a waited+=1
        if %waited% geq 30 goto parent_gone
        timeout /t 1 > nul
        goto wait_for_parent

        :parent_gone
        "{exe}" self remove
        if %errorlevel% neq 0 (
            echo Uninstallation failed.
            goto finish
        )
        echo Uninstallation successful. Cleaning up...

        rem Antivirus can hold the exe briefly after it exits.
        set /a tries=0
        :delete_exe
        del /f "{exe}" 2>nul
        if not exist "{exe}" (
            echo Executable deleted successfully.
            goto finish
        )
        set /a tries+=1
        if %tries% geq 5 (
            echo Failed to delete the executable. You may need to delete it manually.
            goto finish
        )
        timeout /t 1 > nul
        goto delete_exe

        :finish
        echo Press any key to exit.
        pause > nul
        rem (goto) with no label kills the parser so the script can delete itself.
        (goto) 2>nul & del "%~f0"
        """).strip()


def uninstall() -> NoReturn:
    """Guide the user through the uninstallation process."""
    from .auth import logout

    config.initialize_config()

    console.print("\n[bold]Uninstallation Process:[/bold]")

    logout()
    console.print("Logged out successfully.")

    config.remove_breadcrumb()
    _remove_desktop_shortcut()

    console.print("\n[bold]Manual Cleanup Instructions:[/bold]\n")

    config_dir = os.environ.get('REDFETCH_CONFIG_DIR')
    if config_dir:
        _delete_config_files(Path(config_dir))

    leftover_dirs = _collect_leftover_dirs()
    if leftover_dirs:
        console.print("The following directories may contain files downloaded by redfetch:")
        for path in sorted(leftover_dirs):
            console.print(f" - [cyan]{path}[/cyan]")
        commands = generate_removal_commands(leftover_dirs)
        write_commands_to_file(commands, leftover_dirs)
    else:
        console.print("[green]No existing directories found that need manual cleanup.[/green]\n")

    if os.getenv('PYAPP'):
        if Confirm.ask("Would you like to uninstall redfetch's little python environment?"):
            self_remove()
        else:
            console.print("[yellow]Uninstallation canceled.[/yellow]")
    else:
        _print_package_uninstall_hint()
    sys.exit(0)


def _remove_desktop_shortcut() -> None:
    if sys.platform != "win32":
        return
    from . import desktop_shortcut

    try:
        if desktop_shortcut.get_shortcut_path().exists():
            desktop_shortcut.remove_shortcut()
            console.print("Desktop shortcut removed.")
    except Exception as e:
        console.print(f"[yellow]Could not remove the desktop shortcut: {e}[/yellow]")


def _delete_config_files(config_dir: Path) -> None:
    files_to_delete = [config_dir / '.env', config_dir / 'settings.local.toml']
    files_to_delete.extend(config_dir.glob('*.db'))

    for file_path in files_to_delete:
        try:
            file_path.unlink(missing_ok=True)
        except Exception as e:
            console.print(f"[red]Failed to delete {file_path}: {e}[/red]")

    cache_dir = config_dir / '.cache'
    if cache_dir.is_dir():
        try:
            shutil.rmtree(cache_dir)
        except Exception as e:
            console.print(f"[red]Failed to delete cache directory: {e}[/red]")
            # Provide extra context for common Windows multi-user / shared-dir scenarios
            if os.name == "nt" and getattr(e, "winerror", None) == 32:
                console.print(
                    "[yellow]Windows reports that the cache is in use by another process. "
                    "This often happens when another redfetch instance is still running, "
                    "or when multiple Windows user accounts share the same redfetch folder "
                    "(for example under C:\\Users\\Public\\redfetch).[/yellow]"
                )


def _collect_leftover_dirs() -> set[Path]:
    """Collect existing directories, excluding those nested in another."""
    candidates: set[Path] = set()

    for env in ('DEFAULT', *config.ENVS):
        env_settings = config.settings.from_env(env)

        download_folder = env_settings.get('DOWNLOAD_FOLDER')
        if download_folder:
            candidates.add(_absolute_path(download_folder))

        eq_path = env_settings.get('EQPATH')
        if eq_path:
            candidates.add(_absolute_path(eq_path) / "maps")

        for resource in env_settings.get('SPECIAL_RESOURCES', {}).values():
            custom_path = resource.get('custom_path')
            if custom_path:
                candidates.add(_absolute_path(custom_path))
            default_path = resource.get('default_path')
            if default_path and download_folder:
                candidates.add(_absolute_path(os.path.join(download_folder, default_path)))

    config_dir = os.environ.get('REDFETCH_CONFIG_DIR')
    if config_dir:
        candidates.add(_absolute_path(config_dir))

    return _prune_nested({path for path in candidates if path.exists()})


def _absolute_path(raw: str) -> Path:
    return Path(os.path.abspath(raw))


def _prune_nested(paths: set[Path]) -> set[Path]:
    return {
        path
        for path in paths
        if not any(path != other and path.is_relative_to(other) for other in paths)
    }


def generate_removal_commands(paths: set[Path]) -> list[str]:
    """Generate OS-specific commands to remove the given directories."""
    def deepest_first(path: Path) -> tuple[int, str]:
        return (-len(path.parts), str(path).casefold())

    ordered = sorted(paths, key=deepest_first)
    if platform.system() == 'Windows':
        console.print("[bold]These directories may be removed manually after you make sure there's nothing you need from them, you can do so by running the following PowerShell commands:[/bold]\n")
        commands = []
        for path in ordered:
            escaped_path = str(path).replace("'", "''")
            commands.append(f"Remove-Item -LiteralPath '{escaped_path}' -Recurse -Force")
    else:
        console.print("[bold]You can remove these directories by running the following commands in your terminal:[/bold]\n")
        commands = [f"rm -rf {shlex.quote(str(path))}" for path in ordered]

    for command in commands:
        console.print(f"  {command}")
    console.print("\n[bold yellow]These directories must be removed manually.[/bold yellow]")
    return commands


def write_commands_to_file(commands: list[str], paths: set[Path]) -> None:
    """Write the removal commands and additional information to a text file and open it on Windows."""
    if platform.system() != 'Windows':
        console.print("[yellow]After that, you can remove the redfetch package.[/yellow]")
        return

    lines = [
        "Manual Cleanup Instructions:",
        "The following directories may contain files downloaded by redfetch. You can remove them manually if you want:",
        *(f" - {path}" for path in sorted(paths)),
        "",
        "Make sure there's nothing you want in them. When ready to delete, you can use:",
        "",
        *commands,
    ]
    file_path = Path.home() / "redfetch_removal_commands.txt"
    # The BOM helps older Notepad versions detect UTF-8.
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    try:
        os.startfile(file_path)
    except Exception as e:
        console.print(f"[red]Failed to open the file: {e}[/red]")
        console.print(f"Please open the file manually: [cyan]{file_path}[/cyan]")


def _print_package_uninstall_hint() -> None:
    commands = {
        'pipx': 'pipx uninstall redfetch',
        'uv': 'uv tool uninstall redfetch',
        'pip': 'pip uninstall redfetch',
    }
    command = commands.get(detect_installation_method(), commands['pip'])
    console.print("\n[bold]To uninstall redfetch, please run the following command:[/bold]")
    console.print(f"  [cyan]{command}[/cyan]")
