# standard
import os
import sys
import subprocess
import webbrowser
from pathlib import Path

# third-party
import pyperclip
import requests
from dynaconf import ValidationError
from textual_fspicker import SelectDirectory

# textual framework
from textual import work
from textual.app import App, ComposeResult
from textual.command import Provider, Hit, Hits, DiscoveryHit
from textual.widgets import Footer, Button, Header, Label, Input, Switch, Select, TabbedContent, TabPane, Log
from textual.events import Print
from textual.containers import ScrollableContainer, Center, Container
from textual.reactive import reactive
from textual.worker import Worker, WorkerState, get_current_worker

# local
from redfetch import db
from redfetch import api
from redfetch import config
from redfetch import utils
from redfetch import listener
from redfetch.main import synchronize_db_and_download
from redfetch.__about__ import __version__


# for dev mode, from root dir:
# "hatch shell dev" 
# "textual run --dev .\src\redfetch\main.py"

class RedFetchCommands(Provider):
    """Command provider for RedFetch application."""

    async def startup(self) -> None:
        """Called once when the command palette is opened."""
        pass

    async def search(self, query: str) -> Hits:
        app = self.app
        assert isinstance(app, RedFetch)

        matcher = self.matcher(query)

        commands = [
            ("Update Watched", app.handle_update_watched, "Update all watched & special resources"),
            ("Start RedGuides Interface", app.handle_redguides_interface, "Start the RedGuides interface"),
            ("Stop RedGuides Interface", app.cancel_redguides_interface, "Stop the RedGuides interface"),
            ("Update Single Resource", app.handle_update_resource_id, "Update a single resource by its ID or URL"),
            ("Copy Log", app.handle_copy_log, "Copy the entire log to your clipboard"),
            ("Manage Watched Resources", lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_watched"))), "Manage the resources you're watching"),
            ("Manage Account", lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_account"))), "Manage your RedGuides subscription"),
            ("Licensed Resources", lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_licensed"))), "Manage your purchased resources"),
            ("Open RedGuides Website", lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_redguides"))), "Open the RedGuides website"),
            ("Upgrade to Level 2", lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_ding"))), "Upgrade your RedGuides account to level 2"),
        ]

        for command, action, help_text in commands:
            score = matcher.match(command)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(command),
                    action,
                    help=help_text
                )

    async def discover(self) -> Hits:
        app = self.app
        assert isinstance(app, RedFetch)

        yield DiscoveryHit(
            "Update Watched",
            app.handle_update_watched,
            help="Update all watched & special resources"
        )
        yield DiscoveryHit(
            "Manage Watched Resources",
            lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_watched"))),
            help="Remove resources from your watched list"
        )
        yield DiscoveryHit(
            "Manage Licensed Resources",
            lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_licensed"))),
            help="Manage your purchased resources"
        )
        yield DiscoveryHit(
            "Manage Account",
            lambda: app.on_button_pressed(Button.Pressed(app.query_one("#btn_account"))),
            help="Manage your RedGuides Level 2 subscription"
        )

# the main app class
class RedFetch(App):
    interface_running = False
    is_updating = reactive(False)
    mq_down = reactive(None)
    CSS_PATH = "terminal_ui.tcss"
    current_env = config.settings.ENV 
    download_folder = config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER
    eq_path = config.settings.from_env(config.settings.ENV).EQPATH

    COMMANDS = {RedFetchCommands} | App.COMMANDS
    BINDINGS = [
        ("ctrl+q", "quit", "Quit")
    ]



    def compose(self) -> ComposeResult:
        # this function and the tcss file make up the button placement and styling
        yield Header()
        yield Footer()
        with TabbedContent():
            with TabPane("Fetch", id="fetch"):
                with ScrollableContainer(id="fetch_grid"):
                    with Center(id="center_welcome"):
                        yield Label("Who's this?", id="welcome_label")
                    with Center(id="center_watched"):
                        yield Button("Checking if Very Vanilla MQ is up. 🍦", id="update_watched", variant="default", tooltip="is MQ down?")
                    yield Button("Update Single Resource", id="update_resource_id", variant="default", disabled=True, tooltip="Update a single resource by its ID or URL.")
                    yield Input(placeholder="Paste resource URL or ID", id="resource_id_input", tooltip="Update a single resource by its ID or URL.")
                    yield Button("RedGuides Interface 🌐", id="redguides_interface", variant="default", tooltip="Access an interface for this script on the website.")
                    yield Button("Copy Log", id="copy_log", variant="default", tooltip="Copy the entire log to your clipboard")
                    yield PrintCapturingLog(id="fetch_log", classes="fetch_log")

            with TabPane("Settings", id="settings"):
                with ScrollableContainer(id="settings_grid"):
                    yield Label("Select Server Type:", classes="left_middle")
                    yield Select[str](
                        [("Live", "LIVE"), ("Test", "TEST"), ("Emu", "EMU")],
                        id="server_type",
                        value=self.current_env,  # Use the reactive attribute
                        prompt="Select server type",
                        allow_blank = False,
                        tooltip="The type of EQ server. Live and Test are official servers, while Emu is for unofficial servers."
                    )
                    yield Button("Download Folder", id="select_dl_path", variant="default", tooltip="The base download folder, which by default will contain different versions of VV MQ, MySEQ, and other software.")
                    yield Input(value=config.settings.from_env(self.current_env).DOWNLOAD_FOLDER, placeholder="Paste a basic download directory", id="dl_path_input", tooltip="The base download folder, which by default will contain different versions of VV MQ, MySEQ, and other software.")
                    yield Button("EverQuest Folder", id="select_eq_path", variant="default", tooltip="The EverQuest directory, the one with eqgame.exe. Currently only used to update your maps.")
                    yield Input(value=config.settings.from_env(self.current_env).EQPATH, placeholder="Paste your EverQuest directory", id="eq_path_input", tooltip="The EverQuest directory, the one with eqgame.exe. Currently only used to update your maps.", valid_empty=True)

                    yield Button("Very Vanilla MQ Folder", id="select_vvmq_path", variant="default", tooltip="Your MacroQuest folder.")
                    vvmq_path = utils.get_vvmq_path()
                    if vvmq_path:
                        yield Input(value=vvmq_path, placeholder="Paste your Very Vanilla MQ directory", id="vvmq_path_input", tooltip="The default should be fine, but if you already have a VVMQ install you can select that here.")
                    else:
                        yield Input(value="VVMQ not available for current environment", id="vvmq_path_input", disabled=True)
                    yield Label("Select EQ Map(s):", classes="left_middle")
                    yield Select(
                        [("Brewall's Maps", "brewall"), ("Good's Maps", "good"), ("All", "all")],
                        id="eq_maps",
                        prompt="Select maps",
                        allow_blank=True,
                        value=self.get_current_eq_maps_value(),
                        tooltip="Requires an EverQuest folder above. Adds in-game maps to your 'special resources', with brewall and good's recommended folder structure.",
                    )
                    yield Label("MySEQ:", classes="left_middle")
                    myseq_id = utils.get_current_myseq_id()
                    yield Switch(id="myseq", value=config.settings.from_env(self.current_env).SPECIAL_RESOURCES.get(myseq_id, {}).get('opt_in', False), tooltip="Adds MySEQ to your 'special resources', with maps and offsets for your selected server type.")
                    yield Label("IonBC:", classes="left_middle")
                    yield Switch(id="ionbc", value=config.settings.from_env('DEFAULT').SPECIAL_RESOURCES.get('2463', {}).get('opt_in', False), tooltip="Adds IonBC to your 'special resources'.")
                    yield Button("Clear Download Cache", id="reset_downloads", variant="default", tooltip="This clears a record of what has been downloaded. (it doesn't delete any actual downloads.)")
                    yield Button("Uninstall", id="uninstall", variant="error", tooltip="Uninstall RedFetch and guide through manual cleanup.")
                
            with TabPane("Shortcuts", id="shortcuts"):
                with ScrollableContainer(id="shortcuts_grid"):
                    yield Label("⚡ Run Executables ⚡")
                    yield Button("Very Vanilla MQ 🍦", id="run_macroquest", classes="executable", tooltip="run MacroQuest.exe, the legendary add-on platform for EverQuest.")
                    yield Button("MeshUpdater 🌐", id="run_meshupdater", classes="executable", tooltip="Update EQ zone meshes, needed for MQNav.")
                    yield Button("EQBCS 💬", id="run_eqbcs", classes="executable", tooltip="run EQBCs.exe, the server for eq box chat.")
                    yield Button("EQ LaunchPad 🐲", id="launch_everquest", classes="executable", tooltip="run LaunchPad.exe, the launcher and updater for EverQuest.")
                    yield Button("EQGame 🐲🩹", id="launch_everquest_client", classes="executable", tooltip="run eqgame.exe patchme, which runs the EverQuest client without updating.")
                    yield Button("IonBC 💻", id="run_ionbc", classes="executable", tooltip="run IonBC.exe, a self-contained EQ box chat server for multiple computers that doesn't use MacroQuest.")
                    yield Button("MySEQ 📍", id="run_myseq", classes="executable", tooltip="run MySEQ.exe, a real-time map viewer for EverQuest.")
                    
                    yield Label("📂 Open Folders 📂")
                    yield Button("Downloads 🥏🐕", id="open_dl_folder", classes="folder", tooltip="Open RedFetch downloads folder")
                    yield Button("Very Vanilla MQ 🍦", id="open_vvmq_folder", classes="folder", tooltip="Open MacroQuest folder")
                    yield Button("EverQuest 🐲", id="open_eq_folder", classes="folder", tooltip="Open EverQuest game folder")
                    yield Button("IonBC 💻", id="open_ionbc_folder", classes="folder", tooltip="Open IonBC folder")
                    yield Button("MySEQ 📍", id="open_myseq_folder", classes="folder", tooltip="Open MySEQ folder")

                    yield Label("📎 Open Files 📎")
                    yield Button("settings.local.toml 🥏🐕", id="open_redfetch_config", classes="file", tooltip="Open the RedFetch config file.")
                    yield Button("MacroQuest.ini 🍦", id="open_mq_config", classes="file", tooltip="Open VV MQ's config file.")
                    yield Button("eqclient.ini 🐲", id="open_eq_config", classes="file", tooltip="Open EverQuest's config file.")
                    yield Button("eqhost.txt 🐲", id="open_eq_host", classes="file", tooltip="Open EverQuest's eqhost.txt, which is useful for emulators.")
                    

            with TabPane("Account", id="account"):
                with ScrollableContainer(id="account_grid"):
                    with Center():
                        yield Label("Loading...", id="account_label")
                    with Center():
                        yield Button("Ding for level 2 🆙", id="btn_ding", variant="primary", tooltip="Upgrade your RedGuides account to level 2.")
                        yield Button("Manage Watched Resources 🪺", id="btn_watched", variant="default", classes="web_link", tooltip="Manage the resources you're watching.")
                        yield Button("Licensed Resources 🎫", id="btn_licensed", variant="default", classes="web_link", tooltip="Manage your purchased resources.")
                        yield Button("Manage Account 🧾", id="btn_account", variant="default", classes="web_link", tooltip="Manage your RedGuides 'Level 2' subscription.")
                        yield Button("RedGuides 🍻", id="btn_redguides", variant="default", classes="web_link")

    #
    # events (called by textual framework)
    #

    def on_button_pressed(self, event: Button.Pressed) -> None:

        # fetch
        if event.button.id == "update_watched":
            event.button.variant = "primary"
            self.handle_update_watched()
        elif event.button.id == "update_resource_id":
            event.button.variant = "default"
            self.handle_update_resource_id()
        elif event.button.id == "redguides_interface":
            if not self.interface_running:
                self.interface_running = True
                self.handle_redguides_interface()  # Start the interface
            else:
                self.interface_running = False
                self.cancel_redguides_interface()  # Cancel the interface
        elif event.button.id == "copy_log":
            self.handle_copy_log()

        # settings
        elif event.button.id == "select_dl_path":
            self.select_directory("dl_path_input")
        elif event.button.id == "select_eq_path":
            self.select_directory("eq_path_input")
        elif event.button.id == "select_vvmq_path":
            self.select_directory("vvmq_path_input")
        elif event.button.id == "reset_downloads":
            self.handle_reset_downloads()
        elif event.button.id == "uninstall":
            self.handle_uninstall()

        # shortcuts
        elif event.button.id == "open_dl_folder":
            self.open_folder(utils.get_current_download_folder())
        elif event.button.id == "open_eq_folder":
            self.open_folder(config.settings.from_env(self.current_env).EQPATH)
        elif event.button.id == "open_vvmq_folder":
            self.open_folder(utils.get_vvmq_path())
        elif event.button.id == "run_macroquest":
            self.run_executable(utils.get_vvmq_path(), "MacroQuest.exe")
        elif event.button.id == "launch_everquest":
            self.run_executable(config.settings.from_env(self.current_env).EQPATH, "LaunchPad.exe")
        elif event.button.id == "launch_everquest_client":
            self.run_executable(config.settings.from_env(self.current_env).EQPATH, "eqgame.exe", ["patchme"])
        elif event.button.id == "run_myseq":
            self.run_myseq_executable()
        elif event.button.id == "open_myseq_folder":
            self.open_myseq_folder()
        elif event.button.id == "open_ionbc_folder":
            self.open_ionbc_folder()
        elif event.button.id == "run_ionbc":
            self.run_ionbc_executable()
        elif event.button.id == "run_meshupdater":
            self.run_executable(utils.get_vvmq_path(), "MeshUpdater.exe")
        elif event.button.id == "run_eqbcs":
            self.run_executable(utils.get_vvmq_path(), "EQBCS.exe")
        elif event.button.id == "open_redfetch_config":
            self.open_redfetch_config()
        elif event.button.id == "open_mq_config":
            self.open_mq_config()
        elif event.button.id == "open_eq_config":
            self.open_eq_config()
        elif event.button.id == "open_eq_host":
            self.open_eq_host()

        # account
        elif event.button.id == "btn_watched":
            self.action_link("https://www.redguides.com/community/watched/resources")
        elif event.button.id == "btn_account":
            self.action_link("https://www.redguides.com/amember/member")
        elif event.button.id == "btn_licensed":
            self.action_link("https://www.redguides.com/community/resources/market-place-user/licenses")
        elif event.button.id == "btn_redguides":
            self.action_link("https://www.redguides.com/community")
        elif event.button.id == "btn_ding":
            self.action_link("https://www.redguides.com/amember/member")


    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ["dl_path_input", "eq_path_input", "vvmq_path_input"]:
            input_value = event.input.value.strip()
            self.handle_input_update(event.input.id, input_value)
        elif event.input.id == "resource_id_input":
            self.handle_update_resource_id()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "myseq":
            self.handle_toggle_myseq(event.value)
        elif event.switch.id == "ionbc":
            self.handle_toggle_ionbc(event.value)

    def handle_toggle_dark(self) -> None:
        self.dark = not self.dark  

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "eq_maps":
            new_value = event.value
            if new_value != self.get_current_eq_maps_value():
                self.update_eq_maps_settings(new_value)

        if event.select.id == "server_type":
            new_env = event.value
            if self.current_env != new_env:
                self.current_env = new_env
                config.switch_environment(new_env)
                self.update_widget_states()
                self.check_mq_status_worker()
                self.notify(f"Server type changed to: {new_env}")
            
            # Update the download folder input
            dl_input = self.query_one("#dl_path_input", Input)
            dl_input.value = utils.get_current_download_folder()
            
            # Update eqpath input
            self.eq_path = config.settings.from_env(self.current_env).EQPATH
            eq_input = self.query_one("#eq_path_input", Input)
            eq_input.value = self.eq_path
            
            # Update VVMQ path display
            self.update_vvmq_path_display()
            # Update MySEQ switch state
            self.update_myseq_display()

            eq_maps_select = self.query_one("#eq_maps", Select)
            eq_maps_select.value = self.get_current_eq_maps_value()
            eq_maps_select.disabled = not bool(self.eq_path)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "resource_id_input":
            update_button = self.query_one("#update_resource_id", Button)
            update_button.disabled = not bool(event.value)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.SUCCESS:
            if event.worker.name == "handle_update_watched":
                self.update_complete(event.worker.result, self.query_one("#update_watched", Button))
            elif event.worker.name == "handle_update_resource_id":
                self.update_complete(event.worker.result, self.query_one("#update_resource_id", Button))
            elif event.worker.name == "handle_redguides_interface":
                self.notify("RedGuides Interface is now running.")

        elif event.state == WorkerState.ERROR:
            error_message = f"Worker {event.worker.name} encountered an error: {event.worker.error}"
            self.notify(error_message, severity="error")
            print(error_message)  # Log the error to console as well

            if event.worker.name == "handle_update_watched":
                self.query_one("#update_watched", Button).variant = "error"
            elif event.worker.name == "handle_update_resource_id":
                self.query_one("#update_resource_id", Button).variant = "error"
            elif event.worker.name == "handle_redguides_interface":
                self.query_one("#redguides_interface", Button).variant = "error"
                self.interface_running = False

        elif event.state == WorkerState.CANCELLED:
            self.notify(f"Worker {event.worker.name} was cancelled.", severity="warning")

        # Update widget states based on the current application state
        self.update_widget_states()

    def select_directory(self, input_id: str) -> None:
        # this is an extension of textual by davep. 
        input_widget = self.query_one(f"#{input_id}")
        input_path = input_widget.value.strip()
        
        if input_path:
            path = Path(input_path)
            if path.is_dir():
                start_dir = path
            else:
                self.notify(f"Invalid directory: {input_path}", severity="error")
                start_dir = Path.home()
        else:
            start_dir = Path.home()
        
        self.push_screen(SelectDirectory(location=start_dir), callback=lambda path: self.update_selected_directory(path, input_id))

    #
    # watchers (called by textual framework)
    #

    def watch_is_updating(self, old_value: bool, new_value: bool) -> None:
        self.update_widget_states()

    #
    # action handlers (called by textual framework)
    #

    def action_link (self, href: str) -> None:
        """
        action to invoke webbrowser
        """
        webbrowser.open(href)

    @work(exclusive=True, thread=True, group="generic_group")
    def action_quit(self) -> None:
        if self.interface_running:
            self.interface_running = False

            cancel_complete = self.cancel_redguides_interface()
            cancel_complete.wait()  # Wait for the cancellation to complete
        self.exit()

    #
    # custom handlers
    #

    def handle_input_update(self, input_id: str, input_value: str):
        if input_id == "dl_path_input":
            try:
                config.update_setting(['DOWNLOAD_FOLDER'], input_value, env=self.current_env)
                self.download_folder = input_value
                self.update_vvmq_path_display()
                self.notify("Download folder updated" if input_value else "Download folder cleared")
            except ValidationError as e:
                self.notify(f"Invalid Download Folder: {e}", severity="error")
        elif input_id == "eq_path_input":
            try:
                config.update_setting(['EQPATH'], input_value, env=self.current_env)
                self.eq_path = input_value
                self.notify("EverQuest folder updated" if input_value else "EverQuest folder cleared")
            except ValidationError as e:
                self.notify(f"Invalid EverQuest Path: {e}", severity="error")
            
            # Update EQ maps
            eq_maps_select = self.query_one("#eq_maps", Select)
            eq_maps_select.disabled = not bool(input_value)
            if input_value:
                current_value = eq_maps_select.value
                self.update_eq_maps_settings(current_value)
            else:
                self.update_eq_maps_settings(None)
        elif input_id == "vvmq_path_input":
            vvmq_id = utils.get_current_vvmq_id()
            if vvmq_id:
                try:
                    config.update_setting(['SPECIAL_RESOURCES', vvmq_id, 'custom_path'], input_value, env=self.current_env)
                    self.notify("Very Vanilla MQ folder updated" if input_value else "Very Vanilla MQ folder cleared")
                except ValidationError as e:
                    self.notify(f"Invalid VVMQ Path: {e}", severity="error")

    def handle_copy_log(self) -> None:
        copy_button = self.query_one("#copy_log", Button)
        log_widget = self.query_one("#fetch_log", Log)
        log_content = "\n".join(log_widget.lines)
        pyperclip.copy(log_content)
        self.notify("Log contents copied to clipboard")
        copy_button.variant = "success"
        self.set_timer(3, lambda: self.reset_button("copy_log", "default"))
    
    def reset_button(self, button_id: str, variant: str = "default") -> None:
        # pass the button id and variant to reset
        button = self.query_one(f"#{button_id}", Button)
        button.variant = variant

    def get_current_eq_maps_value(self) -> str:
        if not self.eq_path:
            return Select.BLANK
        
        eq_maps_status = utils.get_eq_maps_status()
        return eq_maps_status if eq_maps_status else Select.BLANK

    def open_folder(self, path: str) -> None:
        """Open a folder in the default file explorer."""
        if os.path.isdir(path):
            try:
                if sys.platform == 'win32':
                    os.startfile(path)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', path])
                else:
                    subprocess.Popen(['xdg-open', path])
            except Exception as e:
                self.notify(f"Failed to open folder: {e}", severity="error")
        else:
            self.notify(f"Directory does not exist: {path}", severity="error")

    def open_file(self, file_path: str, file_name: str) -> None:
        """Open a file in the default application."""
        full_path = os.path.join(file_path, file_name)
        if os.path.isfile(full_path):
            try:
                if sys.platform == 'win32':
                    os.startfile(full_path)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', full_path])
                else:
                    subprocess.Popen(['xdg-open', full_path])
                self.notify(f"{file_name} opened in default text editor.")
            except Exception as e:
                self.notify(f"Failed to open {file_name}: {e}", severity="error")
        else:
            self.notify(f"File not found: {full_path}", severity="error")

    def open_redfetch_config(self) -> None:
        """Open the settings.local.toml file."""
        config_file_path = os.path.join(config.config_dir, 'settings.local.toml')
        config.ensure_config_file_exists(config_file_path)
        # Now open the file
        self.open_file(config.config_dir, 'settings.local.toml')

    def open_mq_config(self) -> None:
        """Open the MacroQuest.ini file."""
        vvmq_path = utils.get_vvmq_path()
        if vvmq_path:
            self.open_file(os.path.join(vvmq_path, 'config'), 'MacroQuest.ini')
        else:
            self.notify("VVMQ path not found.", severity="error")

    def open_eq_config(self) -> None:
        """Open the eqclient.ini file."""
        eq_path = config.settings.from_env(self.current_env).EQPATH
        if eq_path:
            self.open_file(eq_path, 'eqclient.ini')
        else:
            self.notify("EverQuest path not set.", severity="error")

    def open_eq_host(self) -> None:
        """Open the eqhost.txt file."""
        eq_path = config.settings.from_env(self.current_env).EQPATH
        if eq_path:
            self.open_file(eq_path, 'eqhost.txt')
        else:
            self.notify("EverQuest path not set.", severity="error")

    def handle_toggle_myseq(self, value: bool) -> None:
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            current_opt_in = config.settings.from_env(self.current_env).SPECIAL_RESOURCES[myseq_id]['opt_in']
            if current_opt_in != value:
                self.update_myseq_settings(value)

    def handle_toggle_ionbc(self, value: bool) -> None:
        ionbc_id = "2463"  # Static
        current_opt_in = config.settings.from_env('DEFAULT').SPECIAL_RESOURCES[ionbc_id]['opt_in']
        if current_opt_in != value:
            config.update_setting(['SPECIAL_RESOURCES', ionbc_id, 'opt_in'], value, env='DEFAULT')
            state = "enabled" if value else "disabled"
            self.notify(f"IonBC is now {state}")

    def run_executable(self, folder_path: str, executable_name: str, args=None) -> None:
        """Run an executable from a specified folder."""
        if not sys.platform.startswith('win'):
            self.notify("Running executables is only supported on Windows.", severity="error")
            return

        if not folder_path:
            self.notify(f"Folder path not set for {executable_name}", severity="error")
            return

        executable_path = os.path.join(folder_path, executable_name)
        if os.path.isfile(executable_path):
            try:
                if args is None:
                    args = []
                subprocess.Popen([executable_path] + args, cwd=folder_path)
                self.notify(f"{executable_name} started successfully.")
            except Exception as e:
                self.notify(f"Failed to start {executable_name}: {e}", severity="error")
        else:
            self.notify(f"{executable_name} not found in the specified folder.", severity="error")

    def open_myseq_folder(self) -> None:
        """Open the MySEQ folder if available."""
        myseq_path = utils.get_myseq_path()
        if myseq_path and os.path.exists(myseq_path):
            self.open_folder(myseq_path)
        else:
            self.notify("MySEQ path not found.", severity="error")

    def open_ionbc_folder(self) -> None:
        """Open the IonBC folder if available."""
        ionbc_path = utils.get_ionbc_path()
        if ionbc_path and os.path.exists(ionbc_path):
            self.open_folder(ionbc_path)
        else:
            self.notify("IonBC path not found.", severity="error")
    
    def run_ionbc_executable(self) -> None:
        """Run the IonBC executable if available."""
        ionbc_path = utils.get_ionbc_path()
        if ionbc_path:
            ionbc_executable = os.path.join(ionbc_path, "IonBC.exe")  # Adjust executable name if necessary
            if os.path.exists(ionbc_executable):
                self.run_executable(ionbc_path, "IonBC.exe")
            else:
                self.notify("IonBC executable not found.", severity="error")
        else:
            self.notify("IonBC path not found.", severity="error")

    def handle_uninstall(self) -> None:
        from . import meta
        try:
            with self.suspend():
                meta.uninstall()
        except SystemExit:
            print("bye bye!")
            self.exit()

    def run_myseq_executable(self) -> None:
        """Run the MySEQ executable if available."""
        myseq_path = utils.get_myseq_path()
        if myseq_path:
            myseq_executable = os.path.join(myseq_path, "MySEQ.exe")  # Adjust if necessary
            if os.path.exists(myseq_executable):
                self.run_executable(myseq_executable)
            else:
                self.notify("MySEQ executable not found.", severity="error")
        else:
            self.notify("MySEQ path not found.", severity="error")

    def update_widget_states(self):
        """ for updating buttons when a worker is running. """

        # Inputs!
        resource_input = self.query_one("#resource_id_input", Input)
        resource_input.disabled = self.is_updating or self.interface_running
        self.query_one("#eq_path_input", Input).disabled = self.is_updating or self.interface_running
        self.query_one("#dl_path_input", Input).disabled = self.is_updating or self.interface_running
        self.query_one("#vvmq_path_input", Input).disabled = self.is_updating or self.interface_running or not bool(self.download_folder)

        # Buttons!
        update_watched_button = self.query_one("#update_watched", Button)
        # Use self.mq_down to determine the button state
        if self.mq_down is None:
            # MQ status not yet known; disable the button or set to a default state
            update_watched_button.label = "Checking MQ status...📞"
            update_watched_button.tooltip = "Please wait while we check MQ status."
            update_watched_button.disabled = True
        elif self.mq_down:
            update_watched_button.label = "MQ Down - Patch Day 💔"
            update_watched_button.tooltip = (
                "Very Vanilla MQ is down for patch day, check redguides.com for current status."
            )
            update_watched_button.disabled = True
            update_watched_button.variant = "default"
        else:
            update_watched_button.label = "Update Watched & [i]Special[/i] Resources 🍦"
            update_watched_button.tooltip = (
                "Update all resources that you've watched, as well as those we've marked 'special' like Very Vanilla MQ. "
                "(Manage watched resources on the website, and edit 'special' resources in settings.local.toml)"
            )
            update_watched_button.variant = "primary"
            # Also consider other conditions to disable the button
            update_watched_button.disabled = (
                self.is_updating or self.interface_running or not bool(self.download_folder)
            )
        #this changes the size of the button
        update_watched_button.refresh(layout=True)
        self.query_one("#update_resource_id", Button).disabled = self.is_updating or self.interface_running or not bool(self.download_folder) or not bool(resource_input.value)
        redguides_interface_button = self.query_one("#redguides_interface", Button)
        if self.interface_running:
            redguides_interface_button.label = "Stop Interface 🛑"
            redguides_interface_button.tooltip = "RedGuides Interface is currently running, click to stop."
            redguides_interface_button.disabled = False
        else:
            redguides_interface_button.label = "RedGuides Interface 🌐"
            redguides_interface_button.disabled = self.is_updating
        self.query_one("#select_dl_path", Button).disabled = self.is_updating or self.interface_running
        self.query_one("#select_eq_path", Button).disabled = self.is_updating or self.interface_running
        self.query_one("#select_vvmq_path", Button).disabled = self.is_updating or self.interface_running or not bool(self.download_folder)
        self.query_one("#reset_downloads", Button).disabled = self.is_updating or self.interface_running
        self.query_one("#run_macroquest", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_vvmq_path())
        self.query_one("#run_meshupdater", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_vvmq_path())
        self.query_one("#run_eqbcs", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_vvmq_path())
        eq_path = config.settings.from_env(self.current_env).EQPATH
        eq_path_exists = bool(eq_path) and os.path.exists(eq_path)
        self.query_one("#launch_everquest", Button).disabled = self.is_updating or self.interface_running or not eq_path_exists
        self.query_one("#launch_everquest_client", Button).disabled = self.is_updating or self.interface_running or not eq_path_exists
        self.query_one("#open_eq_folder", Button).disabled = self.is_updating or self.interface_running or not eq_path_exists
        myseq_path = utils.get_myseq_path()
        self.query_one("#run_myseq", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_myseq_path())
        self.query_one("#open_myseq_folder", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_myseq_path())
        self.query_one("#run_ionbc", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_ionbc_path())
        self.query_one("#open_ionbc_folder", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_ionbc_path())
        self.query_one("#open_dl_folder", Button).disabled = self.is_updating or self.interface_running or not bool(self.download_folder)
        self.query_one("#uninstall", Button).disabled = self.is_updating or self.interface_running
        self.query_one("#open_vvmq_folder", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_vvmq_path())
        self.query_one("#open_redfetch_config", Button).disabled = self.is_updating or self.interface_running
        self.query_one("#open_mq_config", Button).disabled = self.is_updating or self.interface_running or not bool(utils.get_vvmq_path())
        self.query_one("#open_eq_config", Button).disabled = self.is_updating or self.interface_running or not eq_path_exists
        self.query_one("#open_eq_host", Button).disabled = self.is_updating or self.interface_running or not eq_path_exists

        # Selects!
        self.query_one("#server_type", Select).disabled = self.is_updating or self.interface_running
        eq_maps_select = self.query_one("#eq_maps", Select)
        eq_maps_select.disabled = self.is_updating or self.interface_running or not bool(self.eq_path)
        
        # Switches!
        self.query_one("#myseq", Switch).disabled = self.is_updating or self.interface_running or not bool(utils.get_current_myseq_id())
        self.query_one("#ionbc", Switch).disabled = self.is_updating or self.interface_running

    #
    # setting updaters
    #

    def update_myseq_settings(self, opt_in: bool) -> None:
        # myseq has to figure out its resource id first
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            config.update_setting(['SPECIAL_RESOURCES', myseq_id, 'opt_in'], opt_in, env=self.current_env)
            state = "enabled" if opt_in else "disabled"
            self.notify(f"MySEQ for {self.current_env} is now {state}")
        else:
            self.notify("MySEQ is not available for the current environment", severity="error")
        
    def update_eq_maps_settings(self, selected_value: str | None) -> None:
        # eq maps needed help since we have several options. 
        if selected_value is None or selected_value == Select.BLANK:
            # Handle blank selection
            brewall_opt_in = False
            good_opt_in = False
        else:
            brewall_opt_in = selected_value in ["brewall", "all"]
            good_opt_in = selected_value in ["good", "all"]

        # Update Brewall's Maps setting
        config.update_setting(['SPECIAL_RESOURCES', '153', 'opt_in'], brewall_opt_in, env=self.current_env)

        # Update Good's Maps setting
        config.update_setting(['SPECIAL_RESOURCES', '303', 'opt_in'], good_opt_in, env=self.current_env)

        if selected_value is None or selected_value == Select.BLANK:
            self.notify("EQ Maps settings cleared")
        else:
            self.notify(f"EQ Maps settings updated: Brewall's Maps: {brewall_opt_in}, Good's Maps: {good_opt_in}") 

    def update_selected_directory(self, selected_path: Path | None, input_id: str) -> None:
        if selected_path:
            input_widget = self.query_one(f"#{input_id}")
            input_widget.value = str(selected_path)
            self.notify(f"Directory selected: {selected_path}")
            self.handle_input_update(input_id, str(selected_path))
        else:
            # Handle the case where no path was selected (e.g., user cancelled the dialog)
            self.notify("No directory selected", severity="warning")

    def update_vvmq_path_display(self):
        vvmq_path = utils.get_vvmq_path()
        vvmq_input_widget = self.query_one("#vvmq_path_input", Input)
        if vvmq_path:
            vvmq_input_widget.value = vvmq_path
            vvmq_input_widget.disabled = False
        else:
            vvmq_input_widget.value = "VVMQ not found for this server type."
            vvmq_input_widget.disabled = True

    def update_myseq_display(self):
        myseq_switch = self.query_one("#myseq", Switch)
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            myseq_opt_in = config.settings.from_env(self.current_env).SPECIAL_RESOURCES[myseq_id]['opt_in']
            myseq_switch.value = myseq_opt_in
            myseq_switch.disabled = False
        else:
            myseq_switch.disabled = True
            myseq_switch.value = False

    def update_welcome_label(self, greeting: str):
        welcome_label = self.query_one("#welcome_label", Label)
        welcome_label.update(greeting)

    def update_account_label(self, greetingacct: str):
        welcome_label = self.query_one("#account_label", Label)
        welcome_label.update(greetingacct)

    def show_ding_button(self, show: bool) -> None:
        ding_button = self.query_one("#btn_ding", Button)
        ding_button.display = show
        
    #
    # worker handlers
    #

    @work(thread=True, exclusive=True, group="mq_status_group")
    def check_mq_status_worker(self):
        """Background worker to check MQ status."""
        mq_down = utils.is_mq_down()
        self.call_from_thread(self.set_mq_down_status, mq_down)

    def set_mq_down_status(self, mq_down: bool | None):
        """Set the mq_down reactive variable."""
        self.mq_down = mq_down

    @work(exclusive=True, thread=True, group="generic_group")
    def handle_update_watched(self) -> None:
        self.notify("Updating watched resources...")
        print(f"Starting update of all watched & special resources, please wait...")
        self.is_updating = True
        result = self.run_synchronization()
        self.is_updating = False
        return result 

    def run_synchronization(self, resource_ids=None):
        try:
            # Get the current environment from the server_type Select widget
            server_type_select = self.query_one("#server_type", Select)
            current_env = server_type_select.value

            db_name = f"{current_env}_resources.db"
            db.initialize_db(db_name)
            headers = api.get_api_headers()
            with db.get_db_connection(db_name) as conn:
                cursor = conn.cursor()
                if resource_ids:
                    # Reset download date for specific resources
                    for resource_id in resource_ids:
                        reset_success = self.reset_download_date(cursor, resource_id)
                        if not reset_success:
                            print(f"Failed to reset download date for resource ID: {resource_id}")
                            return False
                # Proceed with synchronization and download
                result = synchronize_db_and_download(cursor, headers, resource_ids=resource_ids)
                return result
        except Exception as e:
            print(f"Error in run_synchronization: {e}")
            return False
        
    def reset_download_date(self, cursor, resource_id):
        try:
            db.reset_download_date_for_resource(cursor, resource_id)
            return True
        except Exception as e:
            print(f"Error during resetting download date for resource ID {resource_id}: {str(e)}")
            return False

    def update_complete(self, result: bool, button: Button):
        if result:
            button.variant = "success"
            self.notify("All resources updated successfully.")
            #clear resource_id_input on success
            if button.id == "update_resource_id":
                input_widget = self.query_one("#resource_id_input", Input)
                input_widget.value = ""
                self.set_timer(6, lambda: self.reset_button("update_resource_id", "default"))
            elif button.id == "update_watched":
                self.set_timer(6, lambda: self.reset_button("update_watched", "primary"))
        else:
            button.variant = "error"
            print(f"Some resources failed to update. You probably forgot to close MacroQuest or eqbcs.exe.")
            self.notify("Failed to update some resources.", severity="error")

    @work(exclusive=True, thread=True, group="generic_group")
    def handle_update_resource_id(self) -> None:
        input_value = self.query_one("#resource_id_input").value
        if input_value:
            try:
                print(f"Downloading resource please wait...")
                resource_id = utils.parse_resource_id(input_value)
                self.notify(f"Updating Resource ID: {resource_id}")
                self.is_updating = True
                result = self.run_synchronization([resource_id])
                self.is_updating = False
                return result
            except ValueError as e:
                self.notify(str(e), severity="error")
                self.is_updating = False
                return False
        else:
            self.notify("Please enter a Resource ID or URL", severity="error")
            return False

    def start_redguides_interface(self):
        db_name = f"{self.current_env}_resources.db"
        headers = api.get_api_headers()
        special_resources = config.settings.from_env(self.current_env).SPECIAL_RESOURCES
        category_map = config.CATEGORY_MAP
        listener.run_server(config.settings.from_env(self.current_env), db_name, headers, special_resources, category_map)
        worker = get_current_worker()
        
        try:
            while True:
                if worker.is_cancelled:
                    print("Worker has been cancelled.")
                    break
        except Exception as e:
            print(f"Exception in worker: {e}")
        finally:
            print("Worker has stopped.")

    def cancel_redguides_interface(self):
        cancelled_workers = self.workers.cancel_group(self, "generic_group")
        #print(f"Cancelled {len(cancelled_workers)} workers in the 'redguides_interface' group.")
        
        # Trigger shutdown
        shutdown_complete = self.trigger_shutdown()
        return shutdown_complete
    
    @work(exclusive=True, thread=True, group="generic_group")
    def handle_reset_downloads(self) -> None:
        self.notify("Resetting all download dates...")
        print("Resetting all download dates, please wait...")
        self.is_updating = True
        result = self.run_reset_downloads()
        self.is_updating = False
        if result:
            self.notify("All download dates have been reset successfully.")
        else:
            self.notify("Failed to reset download dates.", severity="error")

    def run_reset_downloads(self):
        try:
            db_name = f"{self.current_env}_resources.db"
            with db.get_db_connection(db_name) as conn:
                cursor = conn.cursor()
                db.reset_download_dates(cursor)
                conn.commit()
            return True
        except Exception as e:
            print(f"Error in run_reset_downloads: {e}")
            return False

    @work(thread=True)
    def trigger_shutdown(self):
        try:
            response = requests.post('http://localhost:7734/shutdown')
            if response.status_code == 200:
                print("Successfully triggered interface shutdown")
                self.notify("Interface shutdown initiated.")
            else:
                print(f"Failed to trigger server shutdown. Status code: {response.status_code}")
                self.notify("Failed to initiate server shutdown.", severity="error")
        except requests.RequestException as e:
            print(f"Error making request to /shutdown: {e}")
            self.notify("Error communicating with server during shutdown.", severity="error")

    #adding a group to the worker so that it can be cancelled
    @work(exclusive=True, thread=True, group="generic_group")
    def handle_redguides_interface(self) -> None:
        self.notify("Starting RedGuides Interface...")
        self.start_redguides_interface()
        return True
    
    @work(thread=True)
    def load_user_level(self):
        username = api.get_username()
        if api.is_kiss_downloadable(api.get_api_headers()):
            greeting = f"[italic]Hail, [bold]{username}![/bold][/italic]"
            greetingacct = (
                f"[italic][bold]{username}, thank you for being level 2[/bold][/italic] 🫂"
            )
            self.call_from_thread(self.show_ding_button, False)
        else:
            greeting = f"Hey {username}, you're level 1 😞"
            greetingacct = (
                f"Hey {username}, you're level 1 😞 some resources won't be downloaded."
            )
            self.call_from_thread(self.show_ding_button, True)
        # Update the label on the main thread
        self.call_from_thread(self.update_welcome_label, greeting)
        self.call_from_thread(self.update_account_label, greetingacct)

    #
    # the start
    #

    def on_mount(self) -> None:
        # Initialize the Log widget with some content
        log = self.query_one("#fetch_log", Log)
        log.write_line(f"RedFetch v{__version__} allows you to download EQ resources from RedGuides")
        log.write_line("Server type: " + self.current_env)
        log.write_line("\n")
        self.title = "🥏 RedFetch 🐕"
        self.load_user_level()  # background task for welcome message
        self.check_mq_status_worker()

    # 
    # the end
    #

    def on_unmount(self):
        self.interface_running = False
        self.workers.cancel_all()

# display print statements in the log widget
class PrintCapturingLog(Log):
    def on_mount(self) -> None:
        self.begin_capture_print()

    def on_print(self, event: Print) -> None:
        self.write(event.text)

def run_textual_ui():
    app = RedFetch()
    app.run()

if __name__ == "__main__":
    run_textual_ui()

