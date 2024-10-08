import os
import platform
from platformdirs import user_config_dir
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich.box import DOUBLE

console = Console()

def setup_directories():
    default_config_dir = user_config_dir("RedFetch", "RedGuides")
    windows_public_dir = os.path.expandvars(r'%PUBLIC%\RedFetch') if platform.system() == "Windows" else None
    
    options = []
    if windows_public_dir:
        options.append(f"1. Windows Public Directory ({windows_public_dir})")
        options.append(f"2. OS Config Directory ({default_config_dir})")
        options.append("3. Custom Directory")
        default_choice = '1'
    else:
        options.append(f"1. OS Config Directory ({default_config_dir})")
        options.append("2. Custom Directory")
        default_choice = '1'
    
    choice_text = "\n".join(options)
    panel = Panel(
        Text(choice_text, style="cyan"),
        expand=False
    )
    console.print(panel)
    
    choice = Prompt.ask("Enter your choice", choices=[str(i) for i in range(1, len(options) + 1)], default=default_choice)
    
    if (windows_public_dir and choice == '3') or (not windows_public_dir and choice == '2'):
        # Custom directory logic
        custom_dir = Prompt.ask("Enter the path to your custom directory")
        custom_dir = os.path.expanduser(os.path.normpath(custom_dir))
        
        # Check for eqgame.exe in custom_dir or its parent directories
        current_dir = custom_dir
        while current_dir != os.path.dirname(current_dir):  # Stop at root directory
            if 'eqgame.exe' in os.listdir(current_dir):
                console.print(Panel(
                    Text.from_markup(
                        "[bold red blink]WHAT THE FUCK ARE YOU DOING?!?!![/bold red blink]\n"
                        "There's an EQGame.exe in this path!!\n"
                        "Please select a different directory, thank you in advance. :pray:",
                        justify="center"
                    ),
                    title="[bold underline red]Critical Warning[/bold underline red]",
                    border_style="bold red",
                    expand=False
                ))
                return setup_directories()  # Restart the directory selection process
            current_dir = os.path.dirname(current_dir)
        
        if os.path.isdir(custom_dir):
            config_dir = custom_dir
        else:
            console.print(f"[yellow]Directory does not exist: {custom_dir}[/yellow]")
            create_dir = Confirm.ask("Would you like to create this directory?")
            if create_dir:
                try:
                    os.makedirs(custom_dir, exist_ok=True)
                    config_dir = custom_dir
                    console.print(f"[green]Directory created: {custom_dir}[/green]")
                except Exception as e:
                    console.print(f"[bold red]Error creating directory: {e}[/bold red]")
                    console.print("[yellow]Using default config directory.[/yellow]")
                    config_dir = default_config_dir
            else:
                console.print("[yellow]Using default config directory.[/yellow]")
                config_dir = default_config_dir
    elif windows_public_dir and choice == '1':
        config_dir = windows_public_dir
    else:
        config_dir = default_config_dir
    
    os.makedirs(config_dir, exist_ok=True)
    return config_dir

def create_first_run_flag(default_config_dir, chosen_config_dir):
    os.makedirs(default_config_dir, exist_ok=True)
    first_run_flag = os.path.join(default_config_dir, 'first_run_complete')
    with open(first_run_flag, 'w') as f:
        f.write(chosen_config_dir)

def is_first_run(default_config_dir):
    first_run_flag = os.path.join(default_config_dir, 'first_run_complete')
    return not os.path.exists(first_run_flag)

def first_run_setup():
    default_config_dir = user_config_dir("RedFetch", "RedGuides")
    
    if not is_first_run(default_config_dir):
        with open(os.path.join(default_config_dir, 'first_run_complete'), 'r') as f:
            config_dir = f.read().strip()
        
        # Check for .env file
        env_file_path = os.path.join(config_dir, '.env')
        if os.path.exists(env_file_path):
            console.print(Panel(f"[bold yellow]Setup already completed.[/bold yellow]\nConfiguration directory: {config_dir}", expand=False))
            return config_dir
        else:
            console.print("[bold red]Environment file (.env) not found. Rerunning setup.[/bold red]")

    greeting_panel = Panel.fit(
        Text.from_markup(
            ":wave: [bold cyan]Hail and well met![/bold cyan]\n"
            "Where should we put this script's settings file? You may want to mess with it later.",
            justify="center"
        ),
        style="bold white on blue",
        border_style="bright_yellow",
        box=DOUBLE
    )
    console.print(greeting_panel)
    
    config_dir = setup_directories()
    create_first_run_flag(default_config_dir, config_dir)
    console.print(Panel(f"[bold green]Setup complete![/bold green]\nConfiguration directory: {config_dir}", expand=False))
    
    return config_dir

if __name__ == "__main__":
    first_run_setup()