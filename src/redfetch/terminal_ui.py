# standard
import asyncio
import os
import sys
import subprocess
import webbrowser
from pathlib import Path
from itertools import cycle
if sys.platform == 'win32':
    import winreg
else:
    winreg = None

# third-party
import pyperclip
from dynaconf import ValidationError
from textual_fspicker import SelectDirectory
from rich.console import detect_legacy_windows

# textual framework
from textual import work, on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.widgets import Footer, Button, Header, Label, Input, Switch, Select, TabbedContent, TabPane, Log, Static, ProgressBar
from textual.events import Print
from textual.containers import ScrollableContainer, Center, CenterMiddle, Grid, ItemGrid, Vertical
from textual.reactive import reactive
from textual.worker import Worker, WorkerState
from textual.screen import ModalScreen, Screen
from textual.geometry import Offset
from textual.selection import Selection

# local
from redfetch import store
from redfetch import api
from redfetch import config
from redfetch import net
from redfetch import processes
from redfetch import utils
from redfetch import meta
from redfetch import sync

# for dev mode, from root dir:
# "hatch shell dev" 
# "textual run --dev .\src\redfetch\main.py"

class FetchTab(ScrollableContainer):
    """Content for the Fetch tab."""

    def compose(self) -> ComposeResult:
        # Determine input verb based on terminal
        input_verb = "Enter" if detect_legacy_windows() else "Paste"
        current_env = self.app.current_env

        # Simple vertical layout: controls on top, big log on the bottom
        with Vertical(id="fetch_layout"):
            with Grid(id="fetch_grid"):
                yield Select[str](
                    [("Live", "LIVE"), ("Test", "TEST"), ("Emu", "EMU")],
                    id="server_type_fetch",
                    classes="bordertitles",
                    value=current_env,
                    prompt="Select server type",
                    allow_blank=False,
                    tooltip=(
                        "The type of EQ server. Live and Test are official servers, "
                        "while Emu is for unofficial servers."
                    ),
                )
                with CenterMiddle(id="centermiddle_welcome"):
                    with Center(id="center_welcome"):
                        yield Label("Who's this?", id="welcome_label")
                    with Center(id="center_watched"):
                        yield Button(
                            "Checking if Very Vanilla MQ is up. 🍦",
                            id="update_watched",
                            variant="default",
                            tooltip="is MQ down?",
                        )
                yield Static("", id="spacer_for_welcome_centering")
                yield Button(
                    "Update Single Resource",
                    id="update_resource_id",
                    variant="default",
                    disabled=True,
                    tooltip="Update a single resource by its ID or URL.",
                )
                yield Input(
                    placeholder=f"{input_verb} resource URL or ID",
                    id="resource_id_input",
                    tooltip="Update a single resource by its ID or URL.",
                )
                yield ProgressBar(total=None, show_eta=True, id="update_progress", classes="hidden")
            with Vertical(id="fetch_log_container"):
                # Toolbar row with log actions
                with Grid(id="log_toolbar"):
                    yield Input(
                        placeholder="Search log... 🔍",
                        id="log_search",
                        tooltip="Search the log below.",
                    )
                    yield Button(
                        "<-",
                        id="log_search_prev",
                        variant="default",
                        tooltip="Previous log match (N)",
                    )
                    yield Button(
                        "->",
                        id="log_search_next",
                        variant="default",
                        tooltip="Next log match (n)",
                    )
                    yield Button(
                        "Copy Log 📋",
                        id="copy_log",
                        variant="default",
                        tooltip="Copy the entire log to your clipboard.",
                    )
                    yield Button(
                        "Clear Log 🧹",
                        id="clear_log",
                        variant="default",
                        tooltip="Clear all text from the log view.",
                    )
                # Log widget that captures print statements
                yield PrintCapturingLog(id="fetch_log")

    #
    # Log search helpers
    #

    # Log search state (tab-local)
    _log_search_term: str = ""
    _log_search_matches: list[int] = []
    _log_search_index: int = -1

    def _rebuild_log_search_matches(self, term: str) -> None:
        """Recompute all matching line indices for the given term in the fetch log."""
        log = self.query_one("#fetch_log", Log)
        self._log_search_term = term

        if not term:
            self._log_search_matches = []
            self._log_search_index = -1
            self.screen.clear_selection()
            return

        term_lower = term.lower()
        matches: list[int] = []

        for i, line in enumerate(log.lines):
            line_text = str(line)
            if term_lower in line_text.lower():
                matches.append(i)

        self._log_search_matches = matches
        self._log_search_index = -1

    def _show_current_log_search_result(self) -> None:
        """Scroll to and highlight the current search match, if any."""
        log = self.query_one("#fetch_log", Log)

        if not self._log_search_matches or self._log_search_index < 0:
            self.screen.clear_selection()
            return

        line_index = self._log_search_matches[self._log_search_index]
        if line_index >= len(log.lines):
            self.screen.clear_selection()
            return

        line_text = str(log.lines[line_index])
        log.scroll_to(y=line_index, animate=False, immediate=True)

        start = Offset(0, line_index)
        end = Offset(len(line_text), line_index)
        self.screen.selections = {log: Selection(start, end)}

    def _ensure_log_search_matches_current_term(self) -> None:
        """Ensure matches are built for the current value in the search box."""
        search_input = self.query_one("#log_search", Input)
        term = search_input.value
        if term != self._log_search_term:
            self._rebuild_log_search_matches(term)

    def handle_log_search_next(self) -> None:
        """Move to the next search match in the log."""
        self._ensure_log_search_matches_current_term()

        if not self._log_search_matches:
            if self._log_search_term:
                self.app.notify(f"'{self._log_search_term}' not found in log.")
            else:
                self.app.notify("Enter a search term first.")
            return

        self._log_search_index = (
            self._log_search_index + 1
        ) % len(self._log_search_matches)
        self._show_current_log_search_result()

    def handle_log_search_prev(self) -> None:
        """Move to the previous search match in the log."""
        self._ensure_log_search_matches_current_term()

        if not self._log_search_matches:
            if self._log_search_term:
                self.app.notify(f"'{self._log_search_term}' not found in log.")
            else:
                self.app.notify("Enter a search term first.")
            return

        self._log_search_index = (
            self._log_search_index - 1
        ) % len(self._log_search_matches)
        self._show_current_log_search_result()

    def reset_log_search_state(self) -> None:
        """Reset all log search state for this tab."""
        self._log_search_matches = []
        self._log_search_index = -1
        self._log_search_term = ""

    def sync_from_app(self, app: "Redfetch") -> None:
        """Update this tab's widgets from top-level app state."""
        self._update_from_state()

    def _update_from_state(self) -> None:
        """Apply current app state to widgets."""
        app: "Redfetch" = self.app  # type: ignore[assignment]
        busy = app.is_updating
        interface_running = app.interface_running

        # Update watched button - depends on mq_down, is_updating, interface_running, download_folder
        update_watched_button = self.query_one("#update_watched", Button)
        mq_down = app.mq_down
        download_folder = app.download_folder
        if mq_down is None:
            update_watched_button.label = "Checking MQ status...📞"
            update_watched_button.tooltip = "Please wait while we check MQ status."
            update_watched_button.disabled = True
        elif mq_down:
            update_watched_button.label = "MQ Down: Patch Day 💔"
            update_watched_button.tooltip = (
                "Very Vanilla MQ is down for patch day, check redguides.com for current status."
            )
            update_watched_button.disabled = True
            update_watched_button.variant = "default"
        else:
            if app.is_updating:
                update_watched_button.label = "Stop Update 🛑"
                update_watched_button.tooltip = "Update in progress. Click to cancel."
                update_watched_button.disabled = False
            else:
                update_watched_button.label = "Easy Update Button 🍦"
                update_watched_button.tooltip = (
                    "Update all resources that you've watched, as well as those we've marked 'special' like Very Vanilla MQ and other staff picks. "
                    "(Manage watched resources on the website, and opt-in or out of any 'special' resources in settings.local.toml)"
                )
                if update_watched_button.variant not in ["success", "error"]:
                    update_watched_button.variant = "primary"
                update_watched_button.disabled = busy or not bool(download_folder)
            update_watched_button.refresh(layout=True)

        # Resource ID input and button
        resource_input = self.query_one("#resource_id_input", Input)
        resource_input.disabled = busy
        self.query_one("#update_resource_id", Button).disabled = (
            busy or not bool(download_folder) or not bool(resource_input.value)
        )

        # Server type select on Fetch tab
        server_type_fetch = self.query_one("#server_type_fetch", Select)
        server_type_fetch.disabled = busy or interface_running
        if server_type_fetch.value != app.current_env:
            # Prevent recursive Select.Changed events when we sync from app state
            with self.prevent(Select.Changed):
                server_type_fetch.value = app.current_env

    # Reactive watchers – keep widgets in sync when tab-local state changes

    def _watch_busy(self, old: bool, new: bool) -> None:
        self._update_from_state()

    def _watch_mq_down(self, old: bool | None, new: bool | None) -> None:
        self._update_from_state()

    def _watch_download_folder(self, old: str, new: str) -> None:
        self._update_from_state()

    def _watch_current_env(self, old: str, new: str) -> None:
        self._update_from_state()

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, "#update_watched")
    def handle_update_watched_pressed(self, event: Button.Pressed) -> None:
        """Handle presses of the 'update_watched' button."""
        if not self.app.is_updating:
            event.button.variant = "primary"
            self.app.handle_update_watched()
        else:
            self.app.cancel_update_watched()

    @on(Button.Pressed, "#update_resource_id")
    def handle_update_resource_id_pressed(self, event: Button.Pressed) -> None:
        """Handle presses of the 'update_resource_id' button."""
        event.button.variant = "default"
        self.app.handle_update_resource_id()

    @on(Button.Pressed, "#log_search_next")
    def handle_log_search_next_pressed(self, event: Button.Pressed) -> None:
        self.handle_log_search_next()

    @on(Button.Pressed, "#log_search_prev")
    def handle_log_search_prev_pressed(self, event: Button.Pressed) -> None:
        self.handle_log_search_prev()

    @on(Button.Pressed, "#copy_log")
    def handle_copy_log_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_copy_log()

    @on(Button.Pressed, "#clear_log")
    def handle_clear_log_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_clear_log()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "resource_id_input":
            self.app.handle_update_resource_id()
        elif event.input.id == "log_search":
            self.handle_log_search_next()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "resource_id_input":
            update_button = self.query_one("#update_resource_id", Button)
            update_button.disabled = not bool(event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "server_type_fetch":
            new_env = event.value
            self.app.current_env = new_env


class SettingsTab(ScrollableContainer):
    """Content for the Settings tab."""

    # Tab-local view of top-level app state
    busy: reactive[bool] = reactive(False)
    download_folder: reactive[str] = reactive("")
    eq_path: reactive[str] = reactive("")
    current_env: reactive[str] = reactive(config.settings.ENV)

    def compose(self) -> ComposeResult:
        input_verb = "Enter" if detect_legacy_windows() else "Paste"
        current_env = self.app.current_env

        with ItemGrid(id="dropdowns_grid"):
            yield Select[str](
                [("Live", "LIVE"), ("Test", "TEST"), ("Emu", "EMU")],
                id="server_type",
                classes="bordertitles",
                value=current_env,
                prompt="Select server type",
                allow_blank=False,
                tooltip=(
                    "The type of EQ server. Live and Test are official servers, "
                    "while Emu is for unofficial servers."
                ),
            )
        with ItemGrid(id="inputs_grid", classes="bordertitles"):
            yield Button(
                "Download Folder",
                id="select_dl_path",
                variant="default",
                tooltip=(
                    "The base download folder, which by default will contain different "
                    "versions of VV MQ, MySEQ, and other software."
                ),
            )
            yield Input(
                value=config.settings.from_env(current_env).DOWNLOAD_FOLDER,
                placeholder=f"{input_verb} a basic download directory",
                id="dl_path_input",
                tooltip=(
                    "The base download folder, which by default will contain different "
                    "versions of VV MQ, MySEQ, and other software."
                ),
            )
            yield Button(
                "EverQuest Folder",
                id="select_eq_path",
                variant="default",
                tooltip=(
                    "The EverQuest directory, the one with eqgame.exe. Currently only "
                    "used to update your maps."
                ),
            )
            yield Input(
                value=config.settings.from_env(current_env).EQPATH,
                placeholder=f"{input_verb} your EverQuest directory",
                id="eq_path_input",
                tooltip=(
                    "The EverQuest directory, the one with eqgame.exe. Currently only "
                    "used to update your maps."
                ),
                valid_empty=True,
            )
            yield Button(
                "Very Vanilla MQ Folder",
                id="select_vvmq_path",
                variant="default",
                tooltip="Your MacroQuest folder.",
            )
            vvmq_path = utils.get_vvmq_path()
            if vvmq_path:
                yield Input(
                    value=vvmq_path,
                    placeholder=f"{input_verb} your Very Vanilla MQ directory",
                    id="vvmq_path_input",
                    tooltip=(
                        "The default should be fine, but if you already have a VVMQ "
                        "install you can select that here."
                    ),
                )
            else:
                yield Input(
                    value="VVMQ not available for current environment",
                    id="vvmq_path_input",
                    disabled=True,
                )
        with ItemGrid(id="special_resources_grid", classes="bordertitles"):
            yield Label("MySEQ:", classes="left_middle")
            myseq_id = utils.get_current_myseq_id()
            yield Switch(
                id="myseq",
                value=config.settings.from_env(current_env)
                .SPECIAL_RESOURCES.get(myseq_id, {})
                .get("opt_in", False),
                tooltip=(
                    "Adds MySEQ to your 'special resources', with maps and offsets "
                    "for your selected server type."
                ),
            )
            yield Label("IonBC:", classes="left_middle")
            yield Switch(
                id="ionbc",
                value=config.settings.from_env("DEFAULT")
                .SPECIAL_RESOURCES.get("2463", {})
                .get("opt_in", False),
                tooltip="Adds IonBC to your 'special resources'.",
            )
            yield Label("Maps:", classes="left_middle")
            yield Select(
                [("Brewall's Maps", "brewall"), ("Good's Maps", "good"), ("All", "all")],
                id="eq_maps",
                prompt="Select maps",
                allow_blank=True,
                value=self.app.get_current_eq_maps_value(),
                tooltip=(
                    "Requires an EverQuest folder. Adds in-game maps to your "
                    "'special resources', with brewall and good's recommended "
                    "folder structure."
                ),
            )
        with ItemGrid(id="settings_grid", classes="bordertitles"):
            yield Label("Close MQ pre-udpate:", classes="left_middle")
            yield Switch(
                id="auto_terminate_processes",
                value=config.settings.from_env(current_env).get(
                    "AUTO_TERMINATE_PROCESSES", None
                ),
                tooltip="Automatically terminate running processes before updates.",
            )
            yield Label("Start MQ post-update:", classes="left_middle")
            yield Switch(
                id="auto_run_vvmq",
                value=config.settings.from_env(current_env).get(
                    "AUTO_RUN_VVMQ", False
                ),
                tooltip="Automatically run Very Vanilla MQ after successful updates.",
            )
        with ItemGrid(id="maintenance_grid", classes="bordertitles"):
            yield Button(
                "Clear Download Cache",
                id="reset_downloads",
                variant="default",
                tooltip=(
                    "This clears a record of what has been downloaded. "
                    "(it doesn't delete any actual downloads.)"
                ),
            )
            yield Button(
                "Uninstall",
                id="uninstall",
                variant="error",
                tooltip="Uninstall redfetch and guide through manual cleanup.",
            )

    def sync_from_app(self, app: "Redfetch") -> None:
        """Copy top-level app state into tab-local reactives."""
        self.busy = app.is_updating or app.interface_running
        self.download_folder = app.download_folder
        self.eq_path = app.eq_path
        self.current_env = app.current_env

    def _update_from_state(self) -> None:
        """Apply current tab-local state to widgets."""
        busy = self.busy

        # Disable entire tab while busy
        self.disabled = busy

        # Path inputs and selection buttons depend on download folder
        has_download = bool(self.download_folder)
        self.query_one("#vvmq_path_input", Input).disabled = not has_download
        self.query_one("#select_vvmq_path", Button).disabled = not has_download

        # Server type select on Settings tab
        server_type = self.query_one("#server_type", Select)
        if server_type.value != self.current_env:
            # Prevent recursive Select.Changed events when we sync from app state
            with self.prevent(Select.Changed):
                server_type.value = self.current_env

        # EQ maps select - depends on eq_path
        eq_maps_select = self.query_one("#eq_maps", Select)
        eq_maps_select.disabled = not bool(self.eq_path)

        # MySEQ switch availability
        self.query_one("#myseq", Switch).disabled = not bool(utils.get_current_myseq_id())

    # Reactive watchers – keep widgets in sync when tab-local state changes

    def _watch_busy(self, old: bool, new: bool) -> None:
        self._update_from_state()

    def _watch_download_folder(self, old: str, new: str) -> None:
        self._update_from_state()

    def _watch_eq_path(self, old: str, new: str) -> None:
        self._update_from_state()

    def _watch_current_env(self, old: str, new: str) -> None:
        # Environment changed – refresh controls from config for the new env.
        settings_for_env = config.settings.from_env(new)

        # Update env-specific switches
        auto_run_vvmq_switch = self.query_one("#auto_run_vvmq", Switch)
        auto_run_vvmq_switch.value = settings_for_env.get("AUTO_RUN_VVMQ", None)

        auto_terminate_switch = self.query_one("#auto_terminate_processes", Switch)
        auto_terminate_switch.value = settings_for_env.get("AUTO_TERMINATE_PROCESSES", None)

        # Update inputs that depend on the current environment
        dl_input = self.query_one("#dl_path_input", Input)
        dl_input.value = utils.get_current_download_folder()

        eq_input = self.query_one("#eq_path_input", Input)
        eq_input.value = self.eq_path

        # Update VVMQ and MySEQ displays for the new environment
        self.update_vvmq_path_display()
        self.update_myseq_display()

        # Update EQ maps select value based on new environment
        eq_maps_select = self.query_one("#eq_maps", Select)
        new_eq_maps_value = self.app.get_current_eq_maps_value()
        if eq_maps_select.value != new_eq_maps_value:
            # Avoid triggering on_select_changed when we are just syncing state
            with self.prevent(Select.Changed):
                eq_maps_select.value = new_eq_maps_value

        # Finally, apply generic state-based updates
        self._update_from_state()

    def update_vvmq_path_display(self) -> None:
        """Update the VVMQ path input based on the current environment."""
        vvmq_path = utils.get_vvmq_path()
        vvmq_input_widget = self.query_one("#vvmq_path_input", Input)
        if vvmq_path:
            vvmq_input_widget.value = vvmq_path
            vvmq_input_widget.disabled = False
        else:
            vvmq_input_widget.value = "VVMQ not found for this server type."
            vvmq_input_widget.disabled = True

    def update_myseq_display(self) -> None:
        """Update the MySEQ switch based on current environment and availability."""
        myseq_switch = self.query_one("#myseq", Switch)
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            myseq_opt_in = (
                config.settings.from_env(self.app.current_env)
                .SPECIAL_RESOURCES[myseq_id]["opt_in"]
            )
            myseq_switch.value = myseq_opt_in
            myseq_switch.disabled = False
        else:
            myseq_switch.disabled = True
            myseq_switch.value = False

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, "#select_dl_path")
    def handle_select_dl_path_pressed(self, event: Button.Pressed) -> None:
        self.app.select_directory("dl_path_input")

    @on(Button.Pressed, "#select_eq_path")
    def handle_select_eq_path_pressed(self, event: Button.Pressed) -> None:
        self.app.select_directory("eq_path_input")

    @on(Button.Pressed, "#select_vvmq_path")
    def handle_select_vvmq_path_pressed(self, event: Button.Pressed) -> None:
        self.app.select_directory("vvmq_path_input")

    @on(Button.Pressed, "#reset_downloads")
    def handle_reset_downloads_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_reset_downloads()

    @on(Button.Pressed, "#uninstall")
    def handle_uninstall_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_uninstall()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ["dl_path_input", "eq_path_input", "vvmq_path_input"]:
            input_value = event.input.value.strip()
            self.app.handle_input_update(event.input.id, input_value)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "myseq":
            self.app.handle_toggle_myseq(event.value)
        elif event.switch.id == "ionbc":
            self.app.handle_toggle_ionbc(event.value)
        elif event.switch.id == "auto_run_vvmq":
            self.app.handle_toggle_auto_run_vvmq(event.value)
        elif event.switch.id == "auto_terminate_processes":
            self.app.handle_toggle_auto_terminate_processes(event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "eq_maps":
            new_value = event.value
            if new_value != self.app.get_current_eq_maps_value():
                self.app.update_eq_maps_settings(new_value)

        if event.select.id == "server_type":
            new_env = event.value
            self.app.current_env = new_env


class ShortcutsTab(ScrollableContainer):
    """Content for the Shortcuts tab."""

    # Tab-local view of top-level app state
    busy: reactive[bool] = reactive(False)
    download_folder: reactive[str] = reactive("")
    eq_path: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        with ItemGrid(id="executables_grid"):
            yield Button(
                "Very Vanilla MQ 🍦",
                id="run_macroquest",
                classes="executable",
                tooltip="Run MacroQuest, the legendary add-on platform for EverQuest.",
            )
            yield Button(
                "MeshUpdater 🌐",
                id="run_meshupdater",
                classes="executable",
                tooltip="Update EQ zone meshes, needed for MQNav.",
            )
            yield Button(
                "EQBCS 💬",
                id="run_eqbcs",
                classes="executable",
                tooltip="run EQBCs.exe, the server for EQ Box Chat (MQ2EQBC).",
            )
            yield Button(
                "EQ LaunchPad 🐲",
                id="launch_everquest",
                classes="executable",
                tooltip="The official launcher and updater for EverQuest.",
            )
            yield Button(
                "EQGame 🐲🩹",
                id="launch_everquest_client",
                classes="executable",
                tooltip="The EverQuest client *WITHOUT* updating.",
            )
            yield Button(
                "IonBC 💻",
                id="run_ionbc",
                classes="executable",
                tooltip=(
                    "run IonBC.exe, a self-contained EQ box chat server for multiple "
                    "computers that doesn't use MacroQuest."
                ),
            )
            yield Button(
                "MySEQ 📍",
                id="run_myseq",
                classes="executable",
                tooltip="run MySEQ.exe, a real-time map viewer for EverQuest.",
            )

        with ItemGrid(id="folders_grid"):
            yield Button(
                "Downloads 📦",
                id="open_dl_folder",
                classes="folder",
                tooltip="Open redfetch downloads folder",
            )
            yield Button(
                "Very Vanilla MQ 🍦",
                id="open_vvmq_folder",
                classes="folder",
                tooltip="Open MacroQuest folder",
            )
            yield Button(
                "EverQuest 🐲",
                id="open_eq_folder",
                classes="folder",
                tooltip="Open EverQuest game folder",
            )
            yield Button(
                "IonBC 💻",
                id="open_ionbc_folder",
                classes="folder",
                tooltip="Open IonBC folder",
            )
            yield Button(
                "MySEQ 📍",
                id="open_myseq_folder",
                classes="folder",
                tooltip="Open MySEQ folder",
            )

        with ItemGrid(id="files_grid"):
            yield Button(
                "settings.local.toml 📦",
                id="open_redfetch_config",
                classes="file",
                tooltip="Open the redfetch config file.",
            )
            yield Button(
                "MacroQuest.ini 🍦",
                id="open_mq_config",
                classes="file",
                tooltip="Open VV MQ's config file.",
            )
            yield Button(
                "eqclient.ini 🐲",
                id="open_eq_config",
                classes="file",
                tooltip="Open EverQuest's config file.",
            )
            yield Button(
                "eqhost.txt 🐲",
                id="open_eq_host",
                classes="file",
                tooltip=(
                    "Open EverQuest's eqhost.txt, which is useful for emulators."
                ),
            )

    def sync_from_app(self, app: "Redfetch") -> None:
        """Copy top-level app state into tab-local reactives."""
        self.busy = app.is_updating
        self.download_folder = app.download_folder
        self.eq_path = app.eq_path

    def _update_from_state(self) -> None:
        """Apply current tab-local state to widgets."""
        busy = self.busy

        # Disable entire tab while busy.
        self.disabled = busy

        # VVMQ-related executables
        vvmq_path = utils.get_vvmq_path()
        self.query_one("#run_macroquest", Button).disabled = (
            not utils.validate_file_in_path(vvmq_path, "MacroQuest.exe")
        )
        self.query_one("#run_meshupdater", Button).disabled = (
            not utils.validate_file_in_path(vvmq_path, "MeshUpdater.exe")
        )
        self.query_one("#run_eqbcs", Button).disabled = (
            not utils.validate_file_in_path(vvmq_path, "EQBCS.exe")
        )

        # EQ-related executables and folders
        eq_path = self.eq_path
        eq_path_exists = bool(eq_path) and os.path.exists(eq_path)
        self.query_one("#launch_everquest", Button).disabled = (
            not utils.validate_file_in_path(eq_path, "LaunchPad.exe")
        )
        self.query_one("#launch_everquest_client", Button).disabled = (
            not utils.validate_file_in_path(eq_path, "eqgame.exe")
        )
        self.query_one("#open_eq_folder", Button).disabled = not eq_path_exists

        # MySEQ
        myseq_path = utils.get_myseq_path()
        self.query_one("#run_myseq", Button).disabled = (
            not utils.validate_file_in_path(myseq_path, "MySEQ.exe")
        )
        self.query_one("#open_myseq_folder", Button).disabled = not bool(myseq_path)

        # IonBC
        ionbc_path = utils.get_ionbc_path()
        self.query_one("#run_ionbc", Button).disabled = (
            not utils.validate_file_in_path(ionbc_path, "IonBC.exe")
        )
        self.query_one("#open_ionbc_folder", Button).disabled = not bool(ionbc_path)

        # Folder shortcuts
        self.query_one("#open_dl_folder", Button).disabled = not bool(self.download_folder)
        self.query_one("#open_vvmq_folder", Button).disabled = not bool(vvmq_path)

    # Reactive watchers – keep widgets in sync when tab-local state changes

    def _watch_busy(self, old: bool, new: bool) -> None:
        self._update_from_state()

    def _watch_download_folder(self, old: str, new: str) -> None:
        self._update_from_state()

    def _watch_eq_path(self, old: str, new: str) -> None:
        self._update_from_state()

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, "#open_dl_folder")
    def handle_open_dl_folder_pressed(self, event: Button.Pressed) -> None:
        self.app.open_folder(utils.get_current_download_folder())

    @on(Button.Pressed, "#open_eq_folder")
    def handle_open_eq_folder_pressed(self, event: Button.Pressed) -> None:
        self.app.open_folder(self.app.eq_path)

    @on(Button.Pressed, "#open_vvmq_folder")
    def handle_open_vvmq_folder_pressed(self, event: Button.Pressed) -> None:
        self.app.open_folder(utils.get_vvmq_path())

    @on(Button.Pressed, "#run_macroquest")
    def handle_run_macroquest_pressed(self, event: Button.Pressed) -> None:
        self.app.run_executable(utils.get_vvmq_path(), "MacroQuest.exe")

    @on(Button.Pressed, "#launch_everquest")
    def handle_launch_everquest_pressed(self, event: Button.Pressed) -> None:
        self.app.run_executable(self.app.eq_path, "LaunchPad.exe")

    @on(Button.Pressed, "#launch_everquest_client")
    def handle_launch_everquest_client_pressed(self, event: Button.Pressed) -> None:
        self.app.run_executable(self.app.eq_path, "eqgame.exe", ["patchme"])

    @on(Button.Pressed, "#run_myseq")
    def handle_run_myseq_pressed(self, event: Button.Pressed) -> None:
        self.app.run_myseq_executable()

    @on(Button.Pressed, "#open_myseq_folder")
    def handle_open_myseq_folder_pressed(self, event: Button.Pressed) -> None:
        self.app.open_myseq_folder()

    @on(Button.Pressed, "#open_ionbc_folder")
    def handle_open_ionbc_folder_pressed(self, event: Button.Pressed) -> None:
        self.app.open_ionbc_folder()

    @on(Button.Pressed, "#run_ionbc")
    def handle_run_ionbc_pressed(self, event: Button.Pressed) -> None:
        self.app.run_ionbc_executable()

    @on(Button.Pressed, "#run_meshupdater")
    def handle_run_meshupdater_pressed(self, event: Button.Pressed) -> None:
        self.app.run_executable(utils.get_vvmq_path(), "MeshUpdater.exe")

    @on(Button.Pressed, "#run_eqbcs")
    def handle_run_eqbcs_pressed(self, event: Button.Pressed) -> None:
        self.app.run_executable(utils.get_vvmq_path(), "EQBCS.exe")

    @on(Button.Pressed, "#open_redfetch_config")
    def handle_open_redfetch_config_pressed(self, event: Button.Pressed) -> None:
        self.app.open_redfetch_config()

    @on(Button.Pressed, "#open_mq_config")
    def handle_open_mq_config_pressed(self, event: Button.Pressed) -> None:
        self.app.open_mq_config()

    @on(Button.Pressed, "#open_eq_config")
    def handle_open_eq_config_pressed(self, event: Button.Pressed) -> None:
        self.app.open_eq_config()

    @on(Button.Pressed, "#open_eq_host")
    def handle_open_eq_host_pressed(self, event: Button.Pressed) -> None:
        self.app.open_eq_host()


class AccountTab(ScrollableContainer):
    """Content for the Account tab."""

    def compose(self) -> ComposeResult:
        with Center():
            yield Label("Loading...", id="account_label")
        with Center():
            yield Button(
                "Ding for level 2 🆙",
                id="btn_ding",
                variant="primary",
                tooltip="Upgrade your RedGuides account to level 2.",
            )
            yield Button(
                "Manage Watched Resources 👀",
                id="btn_watched",
                variant="default",
                classes="web_link",
                tooltip="Manage the resources you're watching.",
            )
            yield Button(
                "Licensed Resources 🎫",
                id="btn_licensed",
                variant="default",
                classes="web_link",
                tooltip="Manage your purchased resources.",
            )
            yield Button(
                "Manage Account 🧾",
                id="btn_account",
                variant="default",
                classes="web_link",
                tooltip="Manage your RedGuides 'Level 2' subscription.",
            )
            yield Button(
                "RedGuides 🍻",
                id="btn_redguides",
                variant="default",
                classes="web_link",
            )

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, "#btn_watched")
    def handle_btn_watched_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community/watched/resources")

    @on(Button.Pressed, "#btn_account")
    def handle_btn_account_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/amember/member")

    @on(Button.Pressed, "#btn_licensed")
    def handle_btn_licensed_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link(
            "https://www.redguides.com/community/resources/market-place-user/licenses"
        )

    @on(Button.Pressed, "#btn_redguides")
    def handle_btn_redguides_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community")

    @on(Button.Pressed, "#btn_ding")
    def handle_btn_ding_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_ding_check()


class MainScreen(Screen):
    """The main screen containing all tabs and UI widgets."""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()
        with TabbedContent():
            with TabPane("Fetch", id="fetch"):
                yield FetchTab(id="fetch_scroll")

            with TabPane("Settings", id="settings"):
                yield SettingsTab(id="settings_scroll")

            with TabPane("Shortcuts", id="shortcuts"):
                yield ShortcutsTab(id="shortcuts_scroll")

            with TabPane("Account", id="account"):
                yield AccountTab(id="account_grid")

    def on_mount(self) -> None:
        """Initialize the screen after widgets are mounted."""
        # Initialize the Log widget with some content
        log = self.query_one("#fetch_log", Log)
        log.write_line(f"redfetch v{meta.get_current_version()} allows you to download resources from RedGuides")
        log.write_line("Server type: " + self.app.current_env)
        log.write_line("\n")

        # Set border titles
        self.query_one("#server_type").border_title = "Server type"
        self.query_one("#server_type_fetch").border_title = "Server type"
        self.query_one("#inputs_grid").border_title = "Directories"
        self.query_one("#settings_grid").border_title = "Settings"
        self.query_one("#special_resources_grid").border_title = "Special Resources"
        self.query_one("#maintenance_grid").border_title = "Maintenance"
        self.query_one("#executables_grid").border_title = "Executables ⚡"
        self.query_one("#folders_grid").border_title = "Folders 📁"
        self.query_one("#files_grid").border_title = "Files 📎"

        # Apply initial enabled/disabled state across the UI using tab-local reactives
        self.sync_tabs_from_app(self.app)  # type: ignore[arg-type]

    #
    # Widget state update methods
    #

    def update_widget_states(self) -> None:
        """Update the state of all widgets based on application state."""
        self.sync_tabs_from_app(self.app)  # type: ignore[arg-type]

    def sync_tabs_from_app(self, app: "Redfetch") -> None:
        """Push top-level app state in to the tab widgets."""
        # Each tab owns its own tab-local reactive view of app state.
        self.query_one(FetchTab).sync_from_app(app)
        self.query_one(SettingsTab).sync_from_app(app)
        self.query_one(ShortcutsTab).sync_from_app(app)

    #
    # UI update helpers
    #

    def update_welcome_label(self, greeting: str) -> None:
        welcome_label = self.query_one("#welcome_label", Label)
        welcome_label.update(greeting)

    def update_account_label(self, greetingacct: str) -> None:
        account_label = self.query_one("#account_label", Label)
        account_label.update(greetingacct)

    def show_ding_button(self, show: bool) -> None:
        ding_button = self.query_one("#btn_ding", Button)
        ding_button.display = show

    def reset_button(self, button_id: str, variant: str = "default") -> None:
        button = self.query_one(f"#{button_id}", Button)
        button.variant = variant
        if button_id == "update_watched":
            vvmq_button = self.query_one("#run_macroquest", Button)
            vvmq_button.styles.border = None

    #
    # Log search proxies (used by key bindings)
    #

    def handle_log_search_next(self) -> None:
        """Proxy: move to the next search match in the log via FetchTab."""
        fetch_tab = self.query_one(FetchTab)
        fetch_tab.handle_log_search_next()

    def handle_log_search_prev(self) -> None:
        """Proxy: move to the previous search match in the log via FetchTab."""
        fetch_tab = self.query_one(FetchTab)
        fetch_tab.handle_log_search_prev()


class Redfetch(App):
    """The main Redfetch application."""

    # Reactive state - initialized with neutral defaults; real values set when MainScreen mounts
    interface_running: reactive[bool] = reactive(False, bindings=True)
    is_updating: reactive[bool] = reactive(False)
    mq_down: reactive[bool | None] = reactive(None)
    download_folder: reactive[str] = reactive("")
    eq_path: reactive[str] = reactive("")
    current_env: reactive[str] = reactive(config.settings.ENV)

    CSS_PATH = "terminal_ui.tcss"

    MODES = {"main": MainScreen}

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+t", "cycle_theme", "Theme"),
        ("ctrl+f", "focus_search", "Search Log"),
        ("ctrl+s", "cycle_server_type", "Server Type"),
        Binding("ctrl+r", "start_interface", "RG.com Interface", tooltip="Download resources while you browse redguides.com"),
        Binding("ctrl+r", "stop_interface", "Stop Interface", tooltip="Other buttons are disabled until you stop the interface"),
        ("n", "search_next"),
        ("N", "search_prev"),
    ]

    def get_system_commands(self, screen: Screen):
        """Add Redfetch-specific commands to the command palette."""
        yield from super().get_system_commands(screen)

        yield SystemCommand(
            "Update Watched",
            "Update all watched & special resources",
            self.handle_update_watched,
            discover=True,
        )
        yield SystemCommand(
            "Manage Watched Resources",
            "Manage the resources you're watching",
            lambda: self.action_link("https://www.redguides.com/community/watched/resources"),
            discover=True,
        )
        yield SystemCommand(
            "Manage Licensed Resources",
            "Manage your purchased resources",
            lambda: self.action_link("https://www.redguides.com/community/resources/market-place-user/licenses"),
            discover=True,
        )
        yield SystemCommand(
            "Manage Account",
            "Manage your RedGuides 'Level 2' subscription",
            lambda: self.action_link("https://www.redguides.com/amember/member"),
            discover=True,
        )
        yield SystemCommand(
            "Start RedGuides Interface",
            "Start the RedGuides.com interface",
            self.action_start_interface,
            discover=not self.interface_running,
        )
        yield SystemCommand(
            "Stop RedGuides Interface",
            "Stop the RedGuides.com interface",
            self.action_stop_interface,
            discover=self.interface_running,
        )
        yield SystemCommand(
            "Update Single Resource",
            "Update a single resource by its ID or URL",
            self.handle_update_resource_id,
            discover=False,
        )
        yield SystemCommand(
            "Copy Log",
            "Copy the entire log to your clipboard",
            self.handle_copy_log,
            discover=False,
        )
        yield SystemCommand(
            "Clear Log",
            "Clear all text from the log",
            self.handle_clear_log,
            discover=False,
        )
        yield SystemCommand(
            "Open RedGuides Website",
            "Open the RedGuides website",
            lambda: self.action_link("https://www.redguides.com/community"),
            discover=False,
        )
        yield SystemCommand(
            "Upgrade to Level 2",
            "Upgrade your RedGuides account to level 2",
            lambda: self.action_link("https://www.redguides.com/amember/member"),
            discover=False,
        )

    async def on_mount(self) -> None:
        """Initialize the app and push the main screen."""
        # Create the theme cycle from available themes
        self.themes = cycle(self.available_themes.keys())

        # Load saved theme preference
        saved_theme = config.settings.get('THEME', 'textual-dark')
        self.theme = saved_theme

        # Initialize reactive state from config
        self.download_folder = config.settings.from_env(self.current_env).DOWNLOAD_FOLDER or ""
        self.eq_path = config.settings.from_env(self.current_env).EQPATH or ""

        # Set app title
        self.title = "  redfetch"

        # Switch to the main mode and wait for it to be fully mounted
        await self.switch_mode("main")

        # Start background tasks after the UI is ready
        self.load_user_level()
        self.check_mq_status_worker()

    def on_unmount(self) -> None:
        self.workers.cancel_all()

    #
    # Watchers - delegate to MainScreen if it's active
    #

    def watch_is_updating(self, old_value: bool, new_value: bool) -> None:
        """Update all widgets when updating state changes."""
        self._update_main_screen()

    def watch_interface_running(self, old_value: bool, new_value: bool) -> None:
        """Update all widgets when interface running state changes."""
        self._update_main_screen()

    def watch_mq_down(self, old: bool | None, new: bool | None) -> None:
        """Update UI when MQ status changes."""
        self._update_main_screen()

    def watch_download_folder(self, old: str, new: str) -> None:
        """Update relevant widgets when the download folder changes."""
        self._update_main_screen()

    def watch_eq_path(self, old: str, new: str) -> None:
        """Update relevant widgets when the EverQuest path changes."""
        self._update_main_screen()

    def watch_current_env(self, old: str, new: str) -> None:
        """Handle changes to the current environment."""
        if old == new:
            return

        # Update configuration for the new environment
        config.switch_environment(new)

        settings_for_env = config.settings.from_env(new)

        # Update reactive paths for the new environment
        self.eq_path = settings_for_env.EQPATH or ""
        # Update environment-specific download folder via helper
        self.download_folder = utils.get_current_download_folder()

        # Apply theme for new environment
        new_theme = settings_for_env.get('THEME', 'textual-dark')
        self.theme = new_theme

        # Re-check MQ status and notify
        self.check_mq_status_worker()
        self.notify(f"Server type changed to: {new}")

        # Let the active screen sync its tab state from the app
        self._update_main_screen()

    def watch_theme(self, theme: str) -> None:
        """Save theme preference when it changes."""
        current_theme = config.settings.get('THEME', 'textual-dark')
        if theme != current_theme:
            try:
                config.update_setting(['THEME'], theme)
            except Exception as e:
                self.notify(f"Failed to save theme preference: {e}", severity="error")

    def _get_main_screen(self) -> MainScreen | None:
        """Get the MainScreen if it's the current screen."""
        if isinstance(self.screen, MainScreen):
            return self.screen
        return None

    def _update_main_screen(self) -> None:
        """Update widget states on the main screen if it's active."""
        main_screen = self._get_main_screen()
        if main_screen:
            main_screen.update_widget_states()

    #
    # Action handlers
    #

    def action_link(self, href: str) -> None:
        """Open a URL in the default browser."""
        webbrowser.open(href)

    def action_quit(self) -> None:
        """Handle the quit action by canceling ongoing workers and exiting."""
        if self.interface_running:
            self.cancel_redguides_interface()
        if self.is_updating:
            self.cancel_update_watched()
        self.exit()

    def action_cycle_server_type(self) -> None:
        """Cycle the server type."""
        if self.is_updating or self.interface_running:
            return

        order = ["LIVE", "TEST", "EMU"]
        try:
            index = order.index(self.current_env)
        except ValueError:
            index = 0
        new_env = order[(index + 1) % len(order)]
        self.current_env = new_env

    def action_focus_search(self) -> None:
        """Focus the log search input."""
        main_screen = self._get_main_screen()
        if main_screen:
            try:
                search_input = main_screen.query_one("#log_search", Input)
                tabbed_content = main_screen.query_one(TabbedContent)
                if tabbed_content.active != "fetch":
                    tabbed_content.active = "fetch"
                search_input.focus()
            except Exception:
                pass

    def action_search_next(self) -> None:
        """Keyboard action: go to next log search match."""
        main_screen = self._get_main_screen()
        if main_screen:
            main_screen.handle_log_search_next()

    def action_search_prev(self) -> None:
        """Keyboard action: go to previous log search match."""
        main_screen = self._get_main_screen()
        if main_screen:
            main_screen.handle_log_search_prev()

    def action_cycle_theme(self) -> None:
        """Cycle to the next theme."""
        new_theme = next(self.themes)
        self.theme = new_theme
        self.notify(f"Theme changed to: {new_theme}")

    def action_start_interface(self) -> None:
        """Start the RedGuides Interface."""
        self.handle_redguides_interface()

    def action_stop_interface(self) -> None:
        """Stop the RedGuides Interface."""
        self.cancel_redguides_interface()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Check if an action may run (dynamic actions)."""
        if action == "start_interface":
            return not self.interface_running  # Hide when running
        if action == "stop_interface":
            return self.interface_running  # Hide when not running
        return True

    #
    # Input handling
    #

    def handle_input_update(self, input_id: str, input_value: str) -> None:
        main_screen = self._get_main_screen()
        if not main_screen:
            return

        if input_id == "dl_path_input":
            try:
                config.update_setting(['DOWNLOAD_FOLDER'], input_value, env=self.current_env)
                self.download_folder = input_value
                settings_tab = main_screen.query_one(SettingsTab)
                settings_tab.update_vvmq_path_display()
                self.notify("Download folder updated" if input_value else "Download folder cleared")
            except ValidationError as e:
                self.notify(f"Invalid Download Folder: {e}", severity="error")
        elif input_id == "eq_path_input":
            if utils.validate_file_in_path(input_value, 'eqgame.exe'):
                try:
                    config.update_setting(['EQPATH'], input_value, env=self.current_env)
                    self.eq_path = input_value
                    self.notify("EverQuest folder updated" if input_value else "EverQuest folder cleared")
                    
                    eq_maps_select = main_screen.query_one("#eq_maps", Select)
                    eq_maps_select.disabled = not bool(input_value)
                    eq_maps_select.value = self.get_current_eq_maps_value()

                except ValidationError as e:
                    self.notify(f"Invalid EverQuest Path: {e}", severity="error")
            else:
                self.notify("Invalid EverQuest folder: eqgame.exe not found", severity="error")
        elif input_id == "vvmq_path_input":
            vvmq_id = utils.get_current_vvmq_id()
            if vvmq_id:
                try:
                    config.update_setting(['SPECIAL_RESOURCES', vvmq_id, 'custom_path'], input_value, env=self.current_env)
                    self.notify("Very Vanilla MQ folder updated" if input_value else "Very Vanilla MQ folder cleared")
                except ValidationError as e:
                    self.notify(f"Invalid VVMQ Path: {e}", severity="error")

    def select_directory(self, input_id: str) -> None:
        """Open a directory picker for the given input."""
        main_screen = self._get_main_screen()
        if not main_screen:
            return

        input_widget = main_screen.query_one(f"#{input_id}")
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

        self.push_screen(
            SelectDirectory(location=start_dir),
            callback=lambda path: self.update_selected_directory(path, input_id)
        )

    def update_selected_directory(self, selected_path: Path | None, input_id: str) -> None:
        main_screen = self._get_main_screen()
        if not main_screen:
            return

        if selected_path:
            input_widget = main_screen.query_one(f"#{input_id}")
            input_widget.value = str(selected_path)
            self.notify(f"Directory selected: {selected_path}")
            self.handle_input_update(input_id, str(selected_path))
        else:
            self.notify("No directory selected", severity="warning")

    #
    # Toggle handlers
    #

    def handle_toggle_myseq(self, value: bool) -> None:
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            current_opt_in = config.settings.from_env(self.current_env).SPECIAL_RESOURCES[myseq_id]['opt_in']
            if current_opt_in != value:
                self.update_myseq_settings(value)

    def handle_toggle_ionbc(self, value: bool) -> None:
        ionbc_id = "2463"
        current_opt_in = config.settings.from_env('DEFAULT').SPECIAL_RESOURCES[ionbc_id]['opt_in']
        if current_opt_in != value:
            config.update_setting(['SPECIAL_RESOURCES', ionbc_id, 'opt_in'], value, env='DEFAULT')
            state = "enabled" if value else "disabled"
            self.notify(f"IonBC is now {state}")

    def handle_toggle_auto_terminate_processes(self, value: bool) -> None:
        main_screen = self._get_main_screen()
        current_value = config.settings.from_env(self.current_env).get('AUTO_TERMINATE_PROCESSES', None)
        if current_value != value:
            config.update_setting(['AUTO_TERMINATE_PROCESSES'], value, env=self.current_env)
            state = "enabled" if value else "disabled"
            self.notify(f"Auto-terminate processes is now {state}")
        if main_screen:
            main_screen.query_one("#auto_terminate_processes", Switch).value = value

    def handle_toggle_auto_run_vvmq(self, value: bool) -> None:
        main_screen = self._get_main_screen()
        current_value = config.settings.from_env(self.current_env).get('AUTO_RUN_VVMQ', None)
        if current_value != value:
            config.update_setting(['AUTO_RUN_VVMQ'], value, env=self.current_env)
            state = "enabled" if value else "disabled"
            self.notify(f"Auto-run VVMQ is now {state}")
        if main_screen:
            main_screen.query_one("#auto_run_vvmq", Switch).value = value

    #
    # Settings updaters
    #

    def update_myseq_settings(self, opt_in: bool) -> None:
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            config.update_setting(['SPECIAL_RESOURCES', myseq_id, 'opt_in'], opt_in, env=self.current_env)
            state = "enabled" if opt_in else "disabled"
            self.notify(f"MySEQ for {self.current_env} is now {state}")
        else:
            self.notify("MySEQ is not available for the current environment", severity="error")

    def update_eq_maps_settings(self, selected_value: str | None) -> None:
        if selected_value is None or selected_value == Select.BLANK:
            brewall_opt_in = False
            good_opt_in = False
        else:
            brewall_opt_in = selected_value in ["brewall", "all"]
            good_opt_in = selected_value in ["good", "all"]

        config.update_setting(['SPECIAL_RESOURCES', '153', 'opt_in'], brewall_opt_in, env=self.current_env)
        config.update_setting(['SPECIAL_RESOURCES', '303', 'opt_in'], good_opt_in, env=self.current_env)

        if selected_value is None or selected_value == Select.BLANK:
            self.notify("EQ Maps settings cleared")
        else:
            self.notify(f"EQ Maps settings updated: Brewall's Maps: {brewall_opt_in}, Good's Maps: {good_opt_in}")

    def get_current_eq_maps_value(self) -> str:
        if not self.eq_path:
            return Select.BLANK
        eq_maps_status = utils.get_eq_maps_status()
        return eq_maps_status if eq_maps_status else Select.BLANK

    #
    # File/folder operations
    #

    def copy_to_clipboard_with_fallback(self, text: str) -> None:
        """Copy text to the clipboard, with a pyperclip fallback on legacy Windows terminals."""
        if detect_legacy_windows():
            try:
                pyperclip.copy(text)
            except Exception as e:
                self.notify(f"Failed to copy to clipboard: {e}", severity="error")
            return
        self.copy_to_clipboard(text)

    def handle_copy_log(self) -> None:
        """Handler for copying log content."""
        main_screen = self._get_main_screen()
        if not main_screen:
            return

        copy_button = main_screen.query_one("#copy_log", Button)
        log_widget = main_screen.query_one("#fetch_log", Log)
        log_content = "\n".join(log_widget.lines)
        self.copy_to_clipboard_with_fallback(log_content)
        self.notify("Log contents copied to clipboard")
        copy_button.variant = "success"
        self.set_timer(3, lambda: setattr(copy_button, "variant", "default"))

    def handle_clear_log(self) -> None:
        """Handler for clearing log content."""
        main_screen = self._get_main_screen()
        if not main_screen:
            return

        clear_button = main_screen.query_one("#clear_log", Button)
        log_widget = main_screen.query_one("#fetch_log", Log)
        log_widget.clear()
        main_screen.clear_selection()

        # Clear FetchTab's log search state
        fetch_tab = main_screen.query_one(FetchTab)
        fetch_tab.reset_log_search_state()
        self.notify("Log cleared")
        clear_button.variant = "success"
        self.set_timer(3, lambda: setattr(clear_button, "variant", "default"))

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
        """Open a file using the default program."""
        full_path = os.path.join(file_path, file_name)
        if not os.path.isfile(full_path):
            self.notify(f"File not found: {full_path}", severity="error")
            return

        if sys.platform == 'win32':
            file_ext = os.path.splitext(file_name)[1].lower()
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, file_ext) as key:
                    winreg.QueryValue(key, "")
                    os.startfile(full_path)
                    self.notify(f"{file_name} opened with default program.")
            except OSError:
                subprocess.Popen(['notepad.exe', full_path])
                self.notify(f"{file_name} opened with Notepad.")
        else:
            try:
                if sys.platform == 'darwin':
                    subprocess.Popen(['open', full_path])
                else:
                    subprocess.Popen(['xdg-open', full_path])
                self.notify(f"{file_name} opened.")
            except Exception:
                file_uri = Path(full_path).as_uri()
                webbrowser.open(file_uri)
                self.notify(f"{file_name} opened in browser.")

    def open_redfetch_config(self) -> None:
        config_file_path = os.path.join(config.config_dir, 'settings.local.toml')
        config.ensure_config_file_exists(config_file_path)
        self.open_file(config.config_dir, 'settings.local.toml')

    def open_mq_config(self) -> None:
        vvmq_path = utils.get_vvmq_path()
        if vvmq_path:
            self.open_file(os.path.join(vvmq_path, 'config'), 'MacroQuest.ini')
        else:
            self.notify("VVMQ path not found.", severity="error")

    def open_eq_config(self) -> None:
        if self.eq_path:
            self.open_file(self.eq_path, "eqclient.ini")
        else:
            self.notify("EverQuest path not set.", severity="error")

    def open_eq_host(self) -> None:
        if self.eq_path:
            self.open_file(self.eq_path, "eqhost.txt")
        else:
            self.notify("EverQuest path not set.", severity="error")

    def open_myseq_folder(self) -> None:
        myseq_path = utils.get_myseq_path()
        if myseq_path and os.path.exists(myseq_path):
            self.open_folder(myseq_path)
        else:
            self.notify("MySEQ path not found.", severity="error")

    def open_ionbc_folder(self) -> None:
        ionbc_path = utils.get_ionbc_path()
        if ionbc_path and os.path.exists(ionbc_path):
            self.open_folder(ionbc_path)
        else:
            self.notify("IonBC path not found.", severity="error")
    
    def run_executable(self, folder_path: str, executable_name: str, args=None) -> None:
        """Run an executable and show appropriate notifications."""
        success = processes.run_executable(folder_path, executable_name, args)
        if success:
            self.notify(f"{executable_name} started successfully.")
        else:
            self.notify(f"Failed to start {executable_name}", severity="error")

    def run_myseq_executable(self) -> None:
        myseq_path = utils.get_myseq_path()
        if myseq_path:
            myseq_executable = os.path.join(myseq_path, "MySEQ.exe")
            if os.path.exists(myseq_executable):
                self.run_executable(myseq_path, "MySEQ.exe")
            else:
                self.notify("MySEQ executable not found.", severity="error")
        else:
            self.notify("MySEQ path not found.", severity="error")

    def run_ionbc_executable(self) -> None:
        ionbc_path = utils.get_ionbc_path()
        if ionbc_path:
            ionbc_executable = os.path.join(ionbc_path, "IonBC.exe")
            if os.path.exists(ionbc_executable):
                self.run_executable(ionbc_path, "IonBC.exe")
            else:
                self.notify("IonBC executable not found.", severity="error")
        else:
            self.notify("IonBC path not found.", severity="error")

    def handle_uninstall(self) -> None:
        """Handle the uninstall button press."""
        def handle_uninstall_response(response: str) -> None:
            if response == UninstallScreen.RESPONSE_YES:
                from . import meta
                try:
                    with self.suspend():
                        meta.uninstall()
                except SystemExit:
                    print("bye bye!")
                    self.exit()
            else:
                username = getattr(self, "username", "You")
                self.notify(f"{username} enjoys clicking things for no reason.")

        self.push_screen(UninstallScreen(), handle_uninstall_response)

    #
    # Worker handlers
    #

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        state = event.state
        group = getattr(worker, "group", None)
        main_screen = self._get_main_screen()

        if state == WorkerState.SUCCESS:
            if worker.name == "_update_watched_worker" and main_screen:
                self.update_complete(worker.result, main_screen.query_one("#update_watched", Button))
            elif worker.name == "_update_single_resource_worker" and main_screen:
                self.update_complete(worker.result, main_screen.query_one("#update_resource_id", Button))
            elif worker.name == "_redguides_interface_worker":
                self.notify("RedGuides Interface is now running.")

        elif state == WorkerState.ERROR:
            error_message = f"Worker {worker.name} encountered an error: {worker.error}"
            self.notify(error_message, severity="error")
            print(error_message)

            if main_screen:
                if worker.name == "_update_watched_worker":
                    main_screen.query_one("#update_watched", Button).variant = "error"
                elif worker.name == "_update_single_resource_worker":
                    main_screen.query_one("#update_resource_id", Button).variant = "error"

        elif state == WorkerState.CANCELLED:
            self.notify(f"Worker {worker.name} was cancelled.", severity="warning")

        if group in {"update_watched_group", "single_update_group", "maintenance_group"}:
            if state in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
                self.is_updating = False

        if group == "interface_group":
            if worker.name == "_redguides_interface_worker":
                if state in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
                    self.interface_running = False
            elif worker.name == "_prepare_redguides_interface_worker":
                if state in {WorkerState.ERROR, WorkerState.CANCELLED}:
                    self.interface_running = False

        self._update_main_screen()

    @work(exclusive=True, group="mq_status_group")
    async def check_mq_status_worker(self):
        """Background worker to check MQ status."""
        mq_down = await net.is_mq_down()
        self.mq_down = mq_down

    def handle_update_watched(self) -> None:
        """Handle the update process for watched resources."""
        if self.is_updating:
            return
        self.notify("Updating watched resources...")
        self.is_updating = True
        self._update_watched_worker()

    @work(exclusive=True, group="update_watched_group")
    async def _update_watched_worker(self) -> bool:
        print("Starting update of all watched & special resources, please wait...")

        mq_folder = utils.get_base_path()
        running_executables = await asyncio.to_thread(processes.are_executables_running_in_folder, mq_folder)
        if running_executables:
            auto_terminate = config.settings.from_env(self.current_env).get('AUTO_TERMINATE_PROCESSES', None)
            if auto_terminate is True:
                await asyncio.to_thread(processes.terminate_executables_in_folder, mq_folder)
            elif auto_terminate is False:
                self.notify("Continuing update without closing processes...", severity="warning")
            else:
                response = await self.push_screen_wait(
                    ProcessTerminationScreen(running_executables=running_executables)
                )

                if response in [ProcessTerminationScreen.RESPONSE_TERMINATE, ProcessTerminationScreen.RESPONSE_ALWAYS]:
                    if response == ProcessTerminationScreen.RESPONSE_ALWAYS:
                        self.handle_toggle_auto_terminate_processes(True)
                    await asyncio.to_thread(processes.terminate_executables_in_folder, mq_folder)
                elif response == ProcessTerminationScreen.RESPONSE_NEVER:
                    self.handle_toggle_auto_terminate_processes(False)
                else:
                    self.notify("Continuing update without closing processes...", severity="warning")

        result = await self.run_synchronization()
        return result
    
    def cancel_update_watched(self):
        cancelled_workers = self.workers.cancel_group(self, "update_watched_group")
        if cancelled_workers:
            self.notify("Update canceled.", severity="warning")

    def on_sync_event(self, event: tuple) -> None:
        """Handle events from the sync process to update the UI."""
        event_type, resource_id, details = event
        self._process_sync_event(event_type, resource_id, details)

    def _process_sync_event(self, event_type: str, resource_id: str | int, details: str | None) -> None:
        """Process sync events on the main thread."""
        main_screen = self._get_main_screen()
        if not main_screen:
            return

        try:
            fetch_tab = main_screen.query_one(FetchTab)
            progress_bar = fetch_tab.query_one("#update_progress", ProgressBar)
            resource_input = fetch_tab.query_one("#resource_id_input", Input)
            
            if event_type == "total":
                total_tasks = int(resource_id)
                if total_tasks > 0:
                    progress_bar.total = total_tasks
                    progress_bar.progress = 0
                    progress_bar.remove_class("hidden")
                    resource_input.add_class("hidden")
            elif event_type == "done":
                progress_bar.advance(1)
        except Exception:
            pass

    async def run_synchronization(self, resource_ids=None):
        try:
            db_name = f"{self.current_env}_resources.db"
            await asyncio.to_thread(store.initialize_db, db_name)
            db_path = store.get_db_path(db_name)
            headers = await api.get_api_headers()
            if resource_ids:
                reset_success = await asyncio.to_thread(
                    store.reset_download_dates_for_resources, db_name, resource_ids
                )
                if not reset_success:
                    return False
            result = await sync.run_sync(db_path, headers, resource_ids=resource_ids, on_event=self.on_sync_event)
            return result
        except Exception as e:
            print(f"Error in run_synchronization: {e}")
            return False

    def update_complete(self, result: bool, button: Button) -> None:
        main_screen = self._get_main_screen()
        
        if main_screen:
            try:
                fetch_tab = main_screen.query_one(FetchTab)
                progress_bar = fetch_tab.query_one("#update_progress", ProgressBar)
                resource_input = fetch_tab.query_one("#resource_id_input", Input)
                progress_bar.add_class("hidden")
                resource_input.remove_class("hidden")
            except Exception:
                pass

        if result:
            button.variant = "success"
            self.notify("All resources updated successfully.")
            if button.id == "update_resource_id" and main_screen:
                input_widget = main_screen.query_one("#resource_id_input", Input)
                input_widget.value = ""
                self.set_timer(6, lambda: main_screen.reset_button("update_resource_id", "default"))
            elif button.id == "update_watched":
                if sys.platform == 'win32':
                    auto_run = config.settings.from_env(self.current_env).get('AUTO_RUN_VVMQ', None)
                    if auto_run is True:
                        self.run_executable(utils.get_vvmq_path(), "MacroQuest.exe")
                        if main_screen:
                            self.set_timer(6, lambda: main_screen.reset_button("update_watched", "primary"))
                    elif auto_run is False:
                        if main_screen:
                            self.set_timer(6, lambda: main_screen.reset_button("update_watched", "primary"))
                    else:
                        def handle_vvmq_response(response: str) -> None:
                            if response in [RunVVMQScreen.RESPONSE_RUN, RunVVMQScreen.RESPONSE_ALWAYS]:
                                if response == RunVVMQScreen.RESPONSE_ALWAYS:
                                    self.handle_toggle_auto_run_vvmq(True)
                                self.run_executable(utils.get_vvmq_path(), "MacroQuest.exe")
                            elif response == RunVVMQScreen.RESPONSE_NEVER:
                                self.handle_toggle_auto_run_vvmq(False)
                            if main_screen:
                                main_screen.reset_button("update_watched", "primary")
                                main_screen.update_widget_states()
                        self.push_screen(RunVVMQScreen(), handle_vvmq_response)
                else:
                    if main_screen:
                        self.set_timer(6, lambda: main_screen.reset_button("update_watched", "primary"))
        else:
            button.variant = "error"
            print("Some resources failed to update.")
            self.notify("Failed to update some resources.", severity="error")

    def handle_update_resource_id(self) -> None:
        main_screen = self._get_main_screen()
        if self.is_updating or not main_screen:
            return

        input_widget = main_screen.query_one("#resource_id_input", Input)
        input_value = input_widget.value.strip()
        if not input_value:
            self.notify("Please enter a Resource ID or URL", severity="error")
            return

        try:
            resource_id = utils.parse_resource_id(input_value)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return

        print("Downloading resource please wait...")
        self.notify(f"Updating Resource ID: {resource_id}")
        self.is_updating = True
        self._update_single_resource_worker(resource_id)

    @work(exclusive=True, group="single_update_group")
    async def _update_single_resource_worker(self, resource_id: str) -> bool:
        result = await self.run_synchronization([resource_id])
        return result

    def cancel_redguides_interface(self):
        self.workers.cancel_group(self, "interface_group")
    
    def handle_reset_downloads(self) -> None:
        if self.is_updating:
            return
        self.notify("Resetting all download dates...")
        self.is_updating = True
        self._reset_downloads_worker()

    @work(exclusive=True, group="maintenance_group")
    async def _reset_downloads_worker(self) -> bool:
        try:
            print("Resetting all download dates, please wait...")
            db_name = f"{self.current_env}_resources.db"
            db_path = store.get_db_path(db_name)
            await store.reset_download_dates_async(db_path)
            self.notify("All download dates have been reset successfully.")
            return True
        except Exception as e:
            print(f"Error in _reset_downloads_worker: {e}")
            self.notify("Failed to reset download dates.", severity="error")
            return False

    def handle_redguides_interface(self) -> None:
        self.interface_running = True
        self.notify("Starting RedGuides Interface...")
        self._prepare_redguides_interface_worker()

    @work(exclusive=True, group="interface_group")
    async def _prepare_redguides_interface_worker(self) -> bool:
        try:
            db_name = f"{self.current_env}_resources.db"
            await asyncio.to_thread(store.initialize_db, db_name)
            headers = await api.get_api_headers()
            settings = config.settings.from_env(self.current_env)
            special_resources = settings.SPECIAL_RESOURCES
            category_map = config.CATEGORY_MAP
        except Exception as exc:
            self.notify(f"Failed to start RedGuides Interface: {exc}", severity="error")
            raise
        else:
            self._redguides_interface_worker(
                settings,
                db_name,
                headers,
                special_resources,
                category_map,
            )
            return True

    @work(exclusive=True, group="interface_group")
    async def _redguides_interface_worker(self, settings, db_name, headers, special_resources, category_map) -> bool:
        from redfetch.listener import run_server_async
        await run_server_async(settings, db_name, headers, special_resources, category_map)
        return True
    
    @work
    async def load_user_level(self):
        main_screen = self._get_main_screen()
        self.username = await api.get_username()
        headers = await api.get_api_headers()
        if await api.is_kiss_downloadable(headers):
            greeting = f"[italic]Hail, [bold]{self.username}![/bold][/italic]"
            greetingacct = (
                f"[italic][bold]{self.username}, thank you for being level 2[/bold][/italic] 💛"
            )
            if main_screen:
                main_screen.show_ding_button(False)
        else:
            greeting = f"Hey {self.username}, you're level 1 😞"
            greetingacct = (
                f"Hey {self.username}, you're level 1 😞 some resources won't be downloaded."
            )
            if main_screen:
                main_screen.show_ding_button(True)
        if main_screen:
            main_screen.update_welcome_label(greeting)
            main_screen.update_account_label(greetingacct)

    def handle_ding_check(self) -> None:
        """Check if user has upgraded to level 2 and update UI accordingly."""
        self.notify("Checking your level... 🎲")
        self._check_ding_level_worker()

    @work(exclusive=True, group="ding_check_group")
    async def _check_ding_level_worker(self) -> None:
        """Worker to check level 2 status and update UI or redirect."""
        main_screen = self._get_main_screen()
        headers = await api.get_api_headers()
        
        # Force a fresh check, bypassing cache
        is_level_2 = await api.is_kiss_downloadable(headers, force_refresh=True)
        
        if is_level_2:
            # User is now level 2! Update the UI
            username = getattr(self, 'username', await api.get_username())
            greeting = f"[italic]Hail, [bold]{username}![/bold][/italic]"
            greetingacct = (
                f"[italic][bold]{username}, thank you for being level 2[/bold][/italic] 💛"
            )
            if main_screen:
                main_screen.show_ding_button(False)
                main_screen.update_welcome_label(greeting)
                main_screen.update_account_label(greetingacct)
            self.notify("🎉 DING! Welcome to level 2!", severity="information")
        else:
            # Still level 1, send them to the upgrade page
            self.notify("You're still level 1. Opening upgrade page...", severity="warning")
            self.action_link("https://www.redguides.com/amember/member")


# display print statements in the log widget
class PrintCapturingLog(Log):
    def on_mount(self) -> None:
        self.begin_capture_print()

    def on_print(self, event: Print) -> None:
        self.write(event.text)


class RunVVMQScreen(ModalScreen):
    """A modal screen to ask if the user wants to run Very Vanilla MQ."""

    RESPONSE_RUN = "run"
    RESPONSE_ALWAYS = "always"
    RESPONSE_NEVER = "never"
    RESPONSE_SKIP = "skip"

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("Run Very Vanilla MQ?", id="question"),
            Button("Yes", variant="primary", id="yesmq"),
            Button("No", variant="default", id="nomq"),
            Center(Button("Always", variant="primary", id="alwaysmq")),
            Center(Button("Never", variant="default", id="nevermq")),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yesmq":
            self.dismiss(self.RESPONSE_RUN)
        elif event.button.id == "alwaysmq":
            self.dismiss(self.RESPONSE_ALWAYS)
        elif event.button.id == "nevermq":
            self.dismiss(self.RESPONSE_NEVER)
        else:
            self.dismiss(self.RESPONSE_SKIP)


class ProcessTerminationScreen(ModalScreen):
    """A modal screen to ask if user wants to terminate running processes."""

    RESPONSE_TERMINATE = "terminate"
    RESPONSE_ALWAYS = "always"
    RESPONSE_NEVER = "never"
    RESPONSE_SKIP = "skip"

    def __init__(self, running_executables: list[tuple[int, str]]):
        super().__init__()
        self.running_executables = running_executables

    def compose(self) -> ComposeResult:
        if any("crashpad" in exe_path.lower() for pid, exe_path in self.running_executables):
            message = "MacroQuest is running, which may interfere with updates."
        else:
            exe_names = ", ".join(os.path.basename(exe_path) for pid, exe_path in self.running_executables)
            message = (
                f"These processes may interfere with updates:\n"
                f"[italic]{exe_names}[/italic]"
            )

        yield Grid(
            Label(message, id="process_message"),
            Label("Attempt to close before updating?", id="close_them"),
            Button("Yes", variant="primary", id="yesterminate"),
            Button("No", variant="default", id="noterminate"),
            Center(Button("Always", variant="primary", id="alwaysterminate")),
            Center(Button("Never", variant="default", id="neverterminate")),
            id="process_dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yesterminate":
            self.dismiss(self.RESPONSE_TERMINATE)
        elif event.button.id == "alwaysterminate":
            self.dismiss(self.RESPONSE_ALWAYS)
        elif event.button.id == "neverterminate":
            self.dismiss(self.RESPONSE_NEVER)
        else:
            self.dismiss(self.RESPONSE_SKIP)


class UninstallScreen(ModalScreen):
    """A modal screen to confirm uninstallation."""

    RESPONSE_YES = "yes"
    RESPONSE_NO = "no"

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("I noticed you pressed the uninstall button.", id="uninstall_message"),
            Label("Was that on purpose?", id="confirm_uninstall"),
            Button("Yes, uninstall redfetch", variant="error", id="yes_uninstall"),
            Button("No, I often click things for no reason.", variant="default", id="no_uninstall"),
            id="uninstall_dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes_uninstall":
            self.dismiss(self.RESPONSE_YES)
        else:
            self.dismiss(self.RESPONSE_NO)


def run_textual_ui():
    app = Redfetch()
    app.run()


if __name__ == "__main__":
    run_textual_ui()
