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
    if executable_path is None:
        raise EnvironmentError("PYAPP environment variable not set.")
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
                if Confirm.ask("Would you like to self-update now?"):
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
                
                if Confirm.ask("Would you like to update now?"):
                    return update_redfetch(update_command, latest_version)
                else:
                    console.print("[yellow]Update skipped. You can manually update later.[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Error checking for updates:[/bold red] {e}")
    print("No update available.")
    return False

def update_redfetch(update_command, latest_version):
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