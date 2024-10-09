import sys
import os
import subprocess
import requests
from packaging import version

# rich
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
from rich.prompt import Confirm

# local
from .__about__ import __version__

TEST_PYPI_URL = "https://test.pypi.org/pypi/redfetch/json"

console = Console()

def get_current_version():
    return __version__

def fetch_latest_version_from_test_pypi():
    response = requests.get(TEST_PYPI_URL)
    response.raise_for_status()
    data = response.json()
    return data['info']['version']

def get_executable_path():
    executable_path = os.environ.get('PYAPP')
    return executable_path

def check_for_update():
    current_version = get_current_version()
    
    try:
        latest_version = fetch_latest_version_from_test_pypi()
        
        if version.parse(latest_version) > version.parse(current_version):
            version_info = Panel(
                Text.assemble(
                    ("An update for RedFetch is available! 🚡\n\n", "bold green"),
                    ("Local version: ", "dim"),
                    (f"{current_version}\n", "cyan"),
                    ("Latest version: ", "dim"),
                    (f"{latest_version}", "cyan bold")
                ),
                title="Update Available",
                expand=False
            )
            console.print(version_info)
            
            if os.getenv('PYAPP'):
                if Confirm.ask("Would you like to update now?"):
                    return self_update()
                else:
                    console.print("[yellow]Update skipped. You can manually update later.[/yellow]")
            else:
                update_command = [
                    sys.executable, 
                    '-m', 
                    'pip', 
                    'install', 
                    '--upgrade', 
                    '--index-url', 'https://test.pypi.org/simple/', 
                    '--extra-index-url', 'https://pypi.org/simple/',  # remove for production
                    'redfetch'
                ]
                
                command_panel = Panel(
                    Text(" ".join(update_command), style="bold cyan"),
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
    print("No update available.")
    return False

def pip_update_redfetch(update_command, latest_version):
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        console.print(f"\n[bold]Updating RedFetch to version {latest_version} in {script_dir}[/bold]")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=console
        ) as progress:
            update_task = progress.add_task("[cyan]Updating RedFetch...", total=100)
            
            process = subprocess.Popen(
                update_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    progress.update(update_task, advance=10)
            
            returncode = process.poll()
        
        if returncode == 0:
            console.print("[bold green]RedFetch has been successfully updated. 🫎[/bold green]")
            return True
        else:
            error_output = process.stderr.read()
            console.print(f"[bold red]Error updating RedFetch:[/bold red] {error_output}")
            return False
    except Exception as e:
        console.print(f"[bold red]Error during update process:[/bold red] {e}")
        return False

def self_update():
    """Update with PYAPP."""
    try:
        console.print("[bold]Performing self-update...[/bold]")

        current_version = get_current_version()
        latest_version = fetch_latest_version_from_test_pypi()
        console.print(f"Current version: {current_version}")
        console.print(f"Latest version: {latest_version}")

        executable_path = get_executable_path()
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

def self_remove():
    """Remove with PYAPP."""
    try:
        console.print("[bold]Performing self-uninstall...[/bold]")

        executable_path = get_executable_path()
        uninstall_command = [executable_path, 'self', 'remove']

        # Start the uninstall process in a new console and exit the current one
        subprocess.Popen(
            uninstall_command,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )

        # Exit the current process to allow the uninstall to proceed
        sys.exit(0)

    except Exception as e:
        console.print(f"[bold red]Error during self-uninstall process:[/bold red] {e}")
        sys.exit(1)

def uninstall():
    """Guide the user through the uninstallation process."""
    # Import required modules when necessary
    import sys
    import os
    import platform
    from rich.console import Console
    from rich.prompt import Confirm
    from . import config

    console = Console()

    console.print("\n[bold]Uninstallation Process:[/bold]")

    # Inform the user of directories that may contain data
    console.print("\n[bold]Manual Cleanup Instructions:[/bold]\n")

    environments = ['DEFAULT', 'LIVE', 'TEST', 'EMU']  # List of environments to check
    printed_paths = set()  # To avoid duplicates
    existing_paths = set()  # Collect existing paths

    def should_print_path(path):
        """Determine if the path should be printed, avoiding nested paths."""
        path = os.path.abspath(path)
        for printed_path in printed_paths:
            try:
                if os.path.commonpath([path, printed_path]) == printed_path:
                    return False
            except ValueError:
                # Paths on different drives; can't have a common path
                continue
        return True

    for env in environments:
        env_settings = config.settings.from_env(env)

        # Get download folder
        download_folder = env_settings.get('DOWNLOAD_FOLDER')
        if download_folder and os.path.exists(download_folder):
            download_folder = os.path.normpath(download_folder)
            if should_print_path(download_folder):
                existing_paths.add(download_folder)
                printed_paths.add(download_folder)

        # Get EQPath
        eq_path = env_settings.get('EQPATH')
        if eq_path:
            eq_path = os.path.normpath(os.path.join(eq_path, "maps"))
            if os.path.exists(eq_path) and should_print_path(eq_path):
                existing_paths.add(eq_path)
                printed_paths.add(eq_path)

        # Special resources
        special_resources = env_settings.get('SPECIAL_RESOURCES', {})
        for resource_id, resource_info in special_resources.items():
            # Get paths from special resources
            custom_path = resource_info.get('custom_path', '')
            default_path = resource_info.get('default_path', '')

            paths = set()

            if custom_path:
                paths.add(os.path.normpath(custom_path))
            if default_path and download_folder:
                paths.add(os.path.normpath(os.path.join(download_folder, default_path)))

            for path in paths:
                if os.path.exists(path) and should_print_path(path):
                    existing_paths.add(path)
                    printed_paths.add(path)

    # Also inform about the configuration directory
    config_dir = os.environ.get('REDFETCH_CONFIG_DIR', '')
    if config_dir and os.path.exists(config_dir) and should_print_path(config_dir):
        existing_paths.add(config_dir)
        printed_paths.add(config_dir)

    if existing_paths:
        console.print("The following directories may contain files downloaded by RedFetch:")
        for path in sorted(existing_paths):
            console.print(f" - [cyan]{path}[/cyan]")

        # Generate OS-specific commands to remove the directories
        generate_removal_commands(existing_paths, console)
    else:
        console.print("[green]No existing directories found that need manual cleanup.[/green]\n")

    # Get executable path
    executable_path = get_executable_path()

    if executable_path:
        # Ask the user if they want to proceed with self-uninstall
        if Confirm.ask("Would you like to proceed with uninstalling RedFetch's little python environment?"):
            # Now, perform self-remove
            self_remove()
        else:
            console.print("[yellow]Uninstallation canceled.[/yellow]")
    else:
        # If executable_path is not set, guide the user to uninstall via pip
        console.print("\n[bold]To uninstall RedFetch, please run the following command:[/bold]")
        console.print("  [cyan]pip uninstall redfetch[/cyan]")
        # Optionally, exit the program
        sys.exit(0)

def generate_removal_commands(paths, console):
    """Generate OS-specific commands to remove the given directories."""
    import platform
    system = platform.system()

    if system == 'Windows':
        # Generate PowerShell commands
        console.print("[bold]These directories need to be removed manually, you can do so by running the following PowerShell commands:[/bold]\n")
        commands = []
        for path in sorted(paths):
            # Escape quotes and handle special characters
            escaped_path = path.replace("'", "''")
            command = f"Remove-Item -LiteralPath '{escaped_path}' -Recurse -Force"
            commands.append(command)
            console.print(f"  {command}")
    else:
        # Assuming Unix-like system
        console.print("[bold]You can remove these directories by running the following commands in your terminal:[/bold]\n")
        commands = []
        for path in sorted(paths):
            # Escape single quotes
            escaped_path = path.replace("'", "'\\''")
            command = f"rm -rf '{escaped_path}'"
            commands.append(command)
            console.print(f"  {command}")
    console.print("\n[bold yellow]These directories must be removed manually.[/bold yellow]")