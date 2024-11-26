# Standard
import os
import platform
import sys
import json

# Third-party
from platformdirs import user_config_dir
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, PromptBase
from rich.text import Text
from rich.box import ASCII2
from tomlkit import load, dumps, document, table, TOMLDocument

# Custom
from redfetch.detecteq import find_everquest_uninstall_location

console = Console()

# zork-like prompt
class CustomPrompt(PromptBase):
    prompt_suffix = " > "

class CustomConfirm(Confirm):
    validate_error_message = "[prompt.invalid]The wizards look at you with a blank stare."

def get_rg_utility_paths():
    """Returns relevant paths from RedGuides Desktop Utility if installed."""
    settings_path = r"C:\ProgramData\RedGuides\DesktopUtility\settings\settings.json"
    relevant_keys = [
        "MQNextInstallLocation",
        "MQNextTestInstallLocation",
        "EmuInstallLocation",
        "MySeqLiveInstallLocation",
        "MySeqTestInstallLocation"
    ]
    
    if not os.path.exists(settings_path):
        return {}
    
    try:
        with open(settings_path, 'r') as f:
            settings = json.load(f)
            return {k: v for k, v in settings.items() 
                   if k in relevant_keys and v}  # Only return non-empty paths
    except Exception:
        return {}

def setup_directories():
    default_config_dir = user_config_dir("redfetch", "RedGuides")
    windows_public_dir = os.path.expandvars(r'%PUBLIC%\redfetch') if platform.system() == "Windows" else None
    
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
                        "[bold red blink]WHAT THE F*** ARE YOU DOING?!?!![/bold red blink]\n"
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
            create_dir = CustomConfirm.ask("Would you like to create this directory?")
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

def load_settings(settings_file):
    """Load existing settings or create a new TOML document."""
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                doc = load(f)
        except Exception as e:
            console.print(f"[bold red]Error loading settings file: {e}[/bold red]")
            doc = document()
    else:
        doc = document()
    return doc

def get_or_create_table(doc: TOMLDocument, table_path: str):
    """Navigate or create nested tables in the TOML document."""
    current_section = doc
    for key_name in table_path.split('.'):
        if key_name not in current_section:
            current_section.add(key_name, table())
        current_section = current_section[key_name]
    return current_section

def update_setting(doc: TOMLDocument, table_path: str, key_name: str, new_value, friendly_name: str):
    """Update a setting in the TOML document with user confirmation."""
    current_section = get_or_create_table(doc, table_path)
    existing_value = current_section.get(key_name, None)

    if existing_value == new_value:
        console.print(f"[green]{friendly_name} already set to:[/green] {new_value}")
        return False
    elif existing_value:
        console.print(f"\n[yellow]Existing {friendly_name} found:[/yellow] {existing_value}")
        console.print(f"[yellow]New {friendly_name} detected:[/yellow] {new_value}")
        if not Confirm.ask(f"Would you like to overwrite the existing {friendly_name} with the new one?"):
            console.print(f"[cyan]Existing {friendly_name} retained.[/cyan]")
            return False
    else:
        console.print(Panel(f"\n[bold green]{friendly_name}: {new_value}[/bold green]", expand=False))

    current_section[key_name] = new_value
    return True

def save_settings(doc: TOMLDocument, settings_file: str):
    """Write the TOML document back to the settings file."""
    try:
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write(dumps(doc))
    except Exception as e:
        console.print(f"[bold red]Error saving settings file: {e}[/bold red]")

def first_run_setup():
    default_config_dir = user_config_dir("redfetch", "RedGuides")
    
    # Check if running in a CI environment
    if os.environ.get('CI') == 'true':
        # Assume setup is complete and use the default config directory
        config_dir = default_config_dir
        os.makedirs(config_dir, exist_ok=True)
        create_first_run_flag(default_config_dir, config_dir)
        return config_dir

    # Load existing settings early
    settings_file = os.path.join(default_config_dir, "settings.local.toml")
    doc = load_settings(settings_file)

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

    # Add EQ detection before greeting
    eq_path = find_everquest_uninstall_location()

    castle_gate = (
        "[red]/ \\               / \\\n[/]"
        "[red]/   \\             /   \\\n[/]"
        "[red]([bright_black]_____[/])           ([bright_black]_____[/])\n[/]"
        "[bright_black]|   |  _   _   _  |   |[/]\n"
        "[bright_black]| O |_| |_| |_| |[/][bright_black]_| O |[/]\n"
        "[bright_black]|-  |[/]          [white]_[/]  [bright_black]| [white]-[/] |[/]\n"
        "[bright_black]|   |   - [red]_^_[/]     |   |[/]\n"
        "[bright_black]|  [white]_[/]|    [red]//[white]|[/]\\\\  [/]- |   |[/]\n"
        "[bright_black]|   |[/]   [red]///[white]|[/]\\\\\\  [/] [bright_black]|  [green])[/]|[/]\n"
        "[bright_black]|-  |_[/][red]  |||[white]|[/]|||[/]   [bright_black]|  [green]([/]|[/]\n"
        "[green])[/][bright_black]   |[/]   [red]|||[white]|[/]|||[/]   [bright_black]|- [green])[/]|[/]\n"
        "[green]([/][bright_black]___|___[/][red]|||[white]|[/]|||[/][bright_black]___|__[green]([/]|[/]\n"
        " [yellow](      ([/]\n"
        "   [yellow]\\      \\\n[/]"
        "     [yellow])      )[/]\n"
        "     [yellow]|      |[/]\n"
        "     [yellow](      ([/]\n"
        "       [yellow]\\      \\\n[/]"
    )

    greeting_panel = Panel.fit(
        Text.from_markup(
            f"{castle_gate}\n"
            "[bright_black]Six wizards, unnaturally identical in their red robes, meet you at the gate. They /wave, and speak in chorus:[/]\n"
            "[bold cyan][italic]\"Hail, and well met, wayfarer. Art thou [/italic][bright_white]\\[ready][/bright_white][italic] to cleave thy soul manifold, and become thyself a great company?\"[/italic][/bold cyan]",
            justify="center"
        ),
        style="white on black",
        border_style="dim bright_red",
        box=ASCII2
    )
    console.print(greeting_panel)
    # Get user response to the wizard
    response = CustomPrompt.ask().lower()

    while any(word in response for word in ["what", "huh", "idk"]) or response == "":
        console.print("\nThe wizards seem annoyed and speak plainly, [italic][bold cyan]\"We're talking about multiboxing EQ. Wanna do that?\"[/italic][/bold cyan]")
        response = CustomPrompt.ask().lower()

    if any(word in response for word in ["ready", "yes", "sure", "yup", "aye", "ok", "okay"]) or response == "y":
        console.print("\nThe wizards /nod and beckon you to enter.")
        console.print("\n[bold cyan][italic]\"Where shall we lay the hall of settings? Thou may wish to tinker with its keys in days yet to come.\"\n[italic][/bold cyan]")
    elif any(word in response for word in ["no", "nope", "nah", "nay"]) or response == "n":
        console.print("\n[bold red]The wizards point to the sky with their longest finger ... \"BEGONE!\"[/bold red]")
        Prompt.ask("\nPress Enter to continue")
        sys.exit(1)
    elif any(phrase in response for phrase in ["xyzzy", "plugh", "hello sailor", "mailbox", "east", "leave house", "grue"]):
        console.print(
            "\nAs you utter the ancient words, the wizards eyes widen."
            "\n[bold cyan][italic]\"Ah, a fellow traveler of twisty passages!\"[/italic][/bold cyan]"
        )
        console.print(
            "\nWith a grand, unified, sweeping gesture, the wizards reveal a hidden passage into the castle."
            "\n[bold cyan][italic]\"Step forth, brave soul, and enter many lives, each fraught with peril and wonder alike.\"[/italic][/bold cyan]"
            "\nThey look at your empty hands and add, [bold cyan][italic]\"You'll need this.\"[/italic][/bold cyan]"
            "\nA brass lantern is summoned into your possession."
        )
        console.print(
            "\n[bold cyan][italic]\"Where shall we record your preferences?\"[/italic][/bold cyan]"
        )
    else:
        console.print("\n[bold red]The wizards shake their heads sadly, \"Your riddle eludes us. Perhaps you should go east.\"[/bold red]")
        Prompt.ask("\nPress Enter to continue")
        sys.exit(1)
    
    config_dir = setup_directories()
    create_first_run_flag(default_config_dir, config_dir)
    console.print(Panel(f"[bold green]Configuration directory: {config_dir}[/bold green]", expand=False))
    
    settings_file = os.path.join(config_dir, "settings.local.toml")
    doc = load_settings(settings_file)

    # Handle EQ path settings
    try:
        existing_eq_path = doc.get("LIVE", {}).get("EQPATH", None)
        if eq_path:
            if existing_eq_path:
                if existing_eq_path == eq_path:
                    console.print(f"\n[green]EQ path is already set to the detected path:[/green] [yellow]{eq_path}[/yellow]")
                else:
                    console.print(f"\n[cyan]Existing EQ path found:[/cyan] [yellow]{existing_eq_path}[/yellow]")
                    console.print(f"\n[bold cyan][italic]\"Behold, an elder realm forged before Napster's rise[/italic]\"[/bold cyan]\n[cyan]{eq_path}[/cyan]")
                    if Confirm.ask("Do you want to overwrite the existing EQ path with the detected one?"):
                        eq_path_updated = update_setting(
                            doc, 'LIVE', 'EQPATH', eq_path, 'EQ path'
                        )
                        if eq_path_updated:
                            save_settings(doc, settings_file)
                            console.print(f"[green]EQ path updated in {settings_file}[/green]")
                        else:
                            console.print(f"[yellow]No changes made to EQ path in {settings_file}[/yellow]")
            else:
                console.print(f"\n[bold cyan][italic]\"Behold, an elder realm forged before Napster's rise[/italic]\":[/bold cyan]")
                console.print(f"\n[yellow]EverQuest detected at:[/yellow]\n[cyan]{eq_path}[/cyan]")
                if CustomConfirm.ask("Use this as your 'Live' EverQuest path?"):
                    eq_path_updated = update_setting(
                        doc, 'LIVE', 'EQPATH', eq_path, 'EQ path'
                    )
                    if eq_path_updated:
                        save_settings(doc, settings_file)
                    else:
                        console.print(f"[yellow]No changes made to EQ path in {settings_file}[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Error handling EQ path settings: {e}[/bold red]")

    # Handle RedGuides Utility paths
    utility_paths = get_rg_utility_paths()
    if not utility_paths:
        return config_dir

    console.print("\n[bold cyan][italic]\"Lo, we have unearthed eld halls of the RedGuides Launcher. Wilt thou make use of this trove?\"[/italic][/bold cyan]")

    # Map utility paths to their corresponding TOML sections and resource IDs
    path_mappings = {
        "MQNextInstallLocation": ("LIVE.SPECIAL_RESOURCES.1974", "Very Vanilla MQ Live"),
        "MQNextTestInstallLocation": ("TEST.SPECIAL_RESOURCES.2218", "Very Vanilla MQ Test"),
        "EmuInstallLocation": ("EMU.SPECIAL_RESOURCES.60", "Very Vanilla MQ Emu"),
        "MySeqLiveInstallLocation": ("LIVE.SPECIAL_RESOURCES.151", "MySEQ Live"),
        "MySeqTestInstallLocation": ("TEST.SPECIAL_RESOURCES.164", "MySEQ Test")
    }

    settings_updated = False
    for path_type, path in utility_paths.items():
        if path_type not in path_mappings:
            continue

        table_path_str, friendly_name = path_mappings[path_type]

        # Retrieve or create the current section in the TOML document
        current_section = get_or_create_table(doc, table_path_str)

        # Set opt_in = true since we detected an existing directory
        if not current_section.get('opt_in', False):
            current_section['opt_in'] = True
            settings_updated = True

        # Retrieve existing path from settings
        existing_path = current_section.get('custom_path', None)

        if existing_path:
            if existing_path == path:
                console.print(f"\n[green]{friendly_name} path is already set to the detected path:[/green] [yellow]{path}[/yellow]")
                continue  # Skip to the next path
            else:
                console.print(f"\n[cyan]Existing {friendly_name} path found in your settings:[/cyan] [yellow]{existing_path}[/yellow]")
                console.print(f"[yellow]{friendly_name} detected at:[/yellow]\n[cyan]{path}[/cyan]")
                prompt_msg = f"Do you want to overwrite the existing {friendly_name} path with the detected one?"
        else:
            console.print(f"\n[yellow]{friendly_name} detected at:[/yellow]\n[cyan]{path}[/cyan]")
            prompt_msg = f"Use this as your {friendly_name} path?"

        if CustomConfirm.ask(prompt_msg):
            current_section['custom_path'] = path
            settings_updated = True
            console.print(Panel(f"[bold green]{friendly_name}: {path}[/bold green]", expand=False))
        else:
            console.print(f"[cyan]No changes made to {friendly_name} path.[/cyan]")

    if settings_updated:
        save_settings(doc, settings_file)

    return config_dir

if __name__ == "__main__":
    first_run_setup()
