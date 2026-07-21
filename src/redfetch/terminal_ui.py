# standard
import asyncio
import os
import sys
import traceback
import webbrowser
from pathlib import Path
from itertools import cycle

# third-party
import httpx
from dynaconf import ValidationError
from textual_fspicker import SelectDirectory
from rich.console import detect_legacy_windows

# textual framework
from textual import work, on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.widgets import Footer, Button, Header, Label, Input, Switch, Select, TabbedContent, TabPane, Log, Static, ProgressBar, RadioSet, RadioButton, Checkbox
from textual.events import Print
from textual.containers import ScrollableContainer, Center, CenterMiddle, Grid, ItemGrid, Vertical, Horizontal
from textual.reactive import reactive
from textual.worker import Worker, WorkerState, WorkerFailed
from textual.screen import ModalScreen, Screen
from textual.geometry import Offset
from textual.selection import Selection

# local
from redfetch import store
from redfetch import api
from redfetch import auth
from redfetch import config
from redfetch import net
from redfetch import post_update
from redfetch import processes
from redfetch import utils
from redfetch import meta
from redfetch import navmesh
from redfetch import sync
from redfetch import shortcuts
from redfetch import desktop_shortcut
from redfetch.sync_types import ExecutionPlan, SyncEvent, SyncOutcome
from redfetch.runtime_errors import display_fatal_error

# for dev mode, from root dir:
# "hatch shell dev" 
# "textual run --dev .\src\redfetch\main.py"


# Tri-state toggle: No / Ask / Yes maps to config values False / None / True.
TRISTATE_OPTIONS: list[tuple[str, bool | None]] = [
    ("No", False),
    ("Ask", None),
    ("Yes", True),
]


def tristate_index(value) -> int:
    """Return the radio index for a stored config value (defaults to Ask)."""
    if value is True:
        return 2
    if value is False:
        return 0
    return 1  # None / unset -> Ask


def tristate_label(value) -> str:
    """Human-readable label for a stored config value."""
    return TRISTATE_OPTIONS[tristate_index(value)][0]


def make_tristate(widget_id: str, value) -> RadioSet:
    """Build a horizontal No/Ask/Yes radio set for ``value``."""
    selected = tristate_index(value)
    return RadioSet(
        *(
            RadioButton(label, value=(i == selected), compact=True)
            for i, (label, _v) in enumerate(TRISTATE_OPTIONS)
        ),
        id=widget_id,
        compact=True,
    )


def set_tristate(radio_set: RadioSet, value) -> None:
    """Select the No/Ask/Yes button matching ``value`` without firing Changed."""
    target = list(radio_set.query(RadioButton))[tristate_index(value)]
    if not target.value:
        target.value = True


def _startup_update_summary(execution_plan: ExecutionPlan) -> tuple[int, str]:
    """Badge count and startup line from the full download total, so both match what pressing Update fetches — a per-reason subset silently omitted install_context_changed re-downloads."""
    count = execution_plan.action_counts().get("download", 0)
    if not count:
        return 0, "Watched resources are up to date."
    # "download" on a first run (nothing held yet); "update" once anything already installed is outdated
    verb = "update" if any(
        action.action == "download" and action.reason == "outdated"
        for action in execution_plan.actions.values()
    ) else "download"
    s = "" if count == 1 else "s"
    return count, f"{count} resource{s} will {verb} if you press the big button."


def make_launch_toggles(selected: set[str]) -> Horizontal:
    """Build a horizontal row of post-update launch checkboxes."""
    return Horizontal(
        *(
            Checkbox(label, value=(value in selected), id=f"launch_{value}", compact=True)
            for value, label in utils.post_update_launch_choices()
        ),
        id="post_update_launch",
    )


def get_staff_pick_ids_for_env(env: str) -> list[str]:
    """Return resource IDs marked as staff_pick in SPECIAL_RESOURCES for the given env."""
    env_settings = config.settings.from_env(env)
    specials = getattr(env_settings, "SPECIAL_RESOURCES", {}) or {}
    if not isinstance(specials, dict):
        return []
    return [
        rid
        for rid, details in specials.items()
        if isinstance(details, dict) and details.get("staff_pick", False)
    ]


def staff_picks_enabled(env: str) -> bool:
    """True when every staff pick for the env is opted in; drives the bundle switch."""
    staff_ids = get_staff_pick_ids_for_env(env)
    specials = config.settings.from_env(env).SPECIAL_RESOURCES
    return bool(staff_ids) and all(specials.get(rid, {}).get("opt_in", False) for rid in staff_ids)


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

    def on_mount(self) -> None:
        for attr in ("mq_down", "is_updating", "progress_visible", "interface_running",
                     "download_folder", "current_env", "_offer_active", "update_count", "watched_flash"):
            self.watch(self.app, attr, self._recompute)
        self.watch(self.app, "username", self._refresh_welcome)
        self.watch(self.app, "is_level_2", self._refresh_welcome)

    def _refresh_welcome(self) -> None:
        app: "Redfetch" = self.app  # type: ignore[assignment]
        if not app.username:
            return  # keep the compose default until identity resolves
        if app.is_level_2 is True:
            greeting = f"[italic]Hail, [bold]{app.username}![/bold][/italic]"
        elif app.is_level_2 is False:
            greeting = f"Hey {app.username}, you're level 1 😞"
        else:
            greeting = f"Hey [bold]{app.username}[/bold]!"
        self.query_one("#welcome_label", Label).update(greeting)

    def _recompute(self) -> None:
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
                if app._offer_active:
                    # sync already finished; the restart/offer flow can't be cancelled
                    update_watched_button.label = "Finishing update... 🏁"
                    update_watched_button.tooltip = "Waiting on the post-update prompts."
                    update_watched_button.disabled = True
                else:
                    update_watched_button.label = "Stop Update 🛑"
                    update_watched_button.tooltip = "Update in progress. Click to cancel."
                    update_watched_button.disabled = False
            else:
                base_tooltip = (
                    "Update all resources that you've watched, as well as those we've marked 'special' like Very Vanilla MQ and other staff picks. "
                    "(Manage watched resources on the website, and opt-in or out of any 'special' resources in settings.local.toml)"
                )
                count = app.update_count
                if count:  # known and > 0: badge it
                    update_watched_button.label = f"Easy Update Button 🍦 ({count})"
                    s = "" if count == 1 else "s"
                    update_watched_button.tooltip = f"{count} resource{s} ready to fetch. {base_tooltip}"
                    resting_variant = "primary"
                else:  # 0 = up to date (calm)
                    update_watched_button.label = "Easy Update Button 🍦"
                    update_watched_button.tooltip = base_tooltip
                    resting_variant = "default" if count == 0 else "primary"
                # a post-sync flash ("success"/"error") wins briefly; otherwise the count-derived resting variant
                update_watched_button.variant = app.watched_flash or resting_variant
                update_watched_button.disabled = busy or not bool(download_folder)
            update_watched_button.refresh(layout=True)

        # Progress bar and resource-id input are a pair: bar shown ⇒ input hidden.
        progress_bar = self.query_one("#update_progress", ProgressBar)
        resource_input = self.query_one("#resource_id_input", Input)
        if app.progress_visible:
            progress_bar.remove_class("hidden")
            resource_input.add_class("hidden")
        else:
            progress_bar.add_class("hidden")
            resource_input.remove_class("hidden")

        # Resource ID input and button
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
                value=config.settings.from_env(current_env).EQPATH or "",
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
            yield Label("Nav Meshes:", classes="left_middle")
            yield Switch(
                id="navmesh",
                value=navmesh.is_navmesh_enabled(),
                tooltip=(
                    "Download pre-made navigation meshes for the Nav plugin (via mqmesh.com). "
                ),
            )
            yield Label("Maps:", classes="left_middle")
            yield Select(
                [("Brewall's Maps", "brewall"), ("Good's Maps", "good"), ("All", "all")],
                id="eq_maps",
                prompt="Select maps",
                allow_blank=True,
                value=self.app.get_current_eq_maps_value(),
                tooltip=(
                    "Requires an EverQuest folder. Adds maps to your "
                    "normal EverQuest map, using Brewall and Good's folders."
                ),
            )
            yield Label("Staff Picks:", classes="left_middle")
            yield Switch(
                id="staff_picks",
                value=staff_picks_enabled(current_env),
                tooltip="A collection of scripts for this server type that RedGuides staff recommends.",
            )
        with ItemGrid(id="settings_grid", classes="bordertitles"):
            yield Label("Background updates:", classes="left_middle")
            yield Switch(
                id="auto_update",
                value=utils.is_auto_update_enabled(),
                tooltip=(
                    "Run an update silently when MacroQuest is launched."
                ),
            )
            yield Label("Start MQ post-update:", classes="left_middle")
            yield make_tristate(
                "auto_run_vvmq",
                config.settings.from_env(current_env).get("AUTO_RUN_VVMQ", None),
            )
            yield Label("Also start post-update:", classes="left_middle")
            yield make_launch_toggles(set(utils.get_post_update_targets(current_env)))
            if sys.platform == "win32":
                yield Label("Desktop shortcut:", classes="left_middle")
                yield Switch(
                    id="desktop_shortcut",
                    value=desktop_shortcut.get_shortcut_path().exists(),
                    tooltip="Create or remove a Desktop shortcut to run redfetch.",
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

    def on_mount(self) -> None:
        # recompute's per-env helpers to read the new env.
        for attr in ("current_env", "download_folder", "eq_path", "is_updating", "interface_running"):
            self.watch(self.app, attr, self._recompute)

    def on_show(self) -> None:
        # Only the shortcut switch needs an fs re-probe; _recompute would clobber path-input text
 
        self._refresh_desktop_shortcut()

    def _recompute(self) -> None:
        """Derive every Settings widget from app state + per-env config."""
        app: "Redfetch" = self.app  # type: ignore[assignment]
        busy = app.is_updating or app.interface_running

        # Disable entire tab while busy
        self.disabled = busy

        # Path inputs and selection buttons depend on download folder
        has_download = bool(app.download_folder)
        self.query_one("#vvmq_path_input", Input).disabled = not has_download
        self.query_one("#select_vvmq_path", Button).disabled = not has_download

        # Server type select on Settings tab
        server_type = self.query_one("#server_type", Select)
        if server_type.value != app.current_env:
            # Prevent recursive Select.Changed events when we sync from app state
            with self.prevent(Select.Changed):
                server_type.value = app.current_env

        # EQ maps select - depends on eq_path
        eq_maps_select = self.query_one("#eq_maps", Select)
        eq_maps_select.disabled = not bool(app.eq_path)

        # MySEQ switch availability
        self.query_one("#myseq", Switch).disabled = not bool(utils.get_current_myseq_id())

        # NavMesh switch - requires VVMQ path to be configured
        self.query_one("#navmesh", Switch).disabled = not bool(utils.get_vvmq_path())

        # Environment-specific settings for the current env
        settings_for_env = config.settings.from_env(app.current_env)

        # Update env-specific switches
        auto_run_vvmq_radio = self.query_one("#auto_run_vvmq", RadioSet)
        with self.prevent(RadioSet.Changed):
            set_tristate(auto_run_vvmq_radio, settings_for_env.get("AUTO_RUN_VVMQ", None))

        # Keep the per-env launch toggles from writing back during app sync.
        enabled_targets = set(utils.get_post_update_targets(app.current_env))
        with self.prevent(Checkbox.Changed):
            for value, _label in utils.post_update_launch_choices():
                checkbox = self.query_one(f"#launch_{value}", Checkbox)
                checkbox.value = value in enabled_targets

        # Setting a switch's value here saves it. prevent() keeps this a display-only refresh.
        with self.prevent(Switch.Changed):
            navmesh_switch = self.query_one("#navmesh", Switch)
            navmesh_switch.value = navmesh.is_navmesh_enabled()

            auto_update_switch = self.query_one("#auto_update", Switch)
            auto_update_switch.value = utils.is_auto_update_enabled()

            staff_switch = self.query_one("#staff_picks", Switch)
            staff_switch.value = staff_picks_enabled(self.app.current_env)

        # Update inputs that depend on the current environment
        dl_input = self.query_one("#dl_path_input", Input)
        dl_input.value = utils.get_current_download_folder()

        eq_input = self.query_one("#eq_path_input", Input)
        eq_input.value = settings_for_env.EQPATH or ""

        # Update VVMQ and MySEQ displays for the current environment
        self.update_vvmq_path_display()
        self.update_myseq_display()

        # Update EQ maps select value based on current environment
        new_eq_maps_value = app.get_current_eq_maps_value()
        if eq_maps_select.value != new_eq_maps_value:
            # Avoid triggering on_select_changed when we are just syncing state
            with self.prevent(Select.Changed):
                eq_maps_select.value = new_eq_maps_value

        self._refresh_desktop_shortcut()

    def _refresh_desktop_shortcut(self) -> None:
        """Sync the Desktop-shortcut switch to the filesystem (win32; no reactive source)."""
        if sys.platform != "win32":
            return
        try:
            shortcut_switch = self.query_one("#desktop_shortcut", Switch)
        except Exception:
            return
        exists = desktop_shortcut.get_shortcut_path().exists()
        if shortcut_switch.value != exists:
            with self.prevent(Switch.Changed):
                shortcut_switch.value = exists

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
        elif event.switch.id == "staff_picks":
            self.app.handle_toggle_staff_picks(event.value)
        elif event.switch.id == "navmesh":
            self.app.handle_toggle_navmesh(event.value)
        elif event.switch.id == "auto_update":
            self.app.handle_toggle_auto_update(event.value)
        elif event.switch.id == "desktop_shortcut":
            self.app.handle_toggle_desktop_shortcut(event.value)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        value = TRISTATE_OPTIONS[event.index][1]
        if event.radio_set.id == "auto_run_vvmq":
            self.app.handle_toggle_auto_run_vvmq(value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "eq_maps":
            new_value = event.value
            if new_value != self.app.get_current_eq_maps_value():
                self.app.update_eq_maps_settings(new_value)

        if event.select.id == "server_type":
            new_env = event.value
            self.app.current_env = new_env

    @on(Checkbox.Changed, "#post_update_launch Checkbox")
    def on_launch_toggle_changed(self, event: Checkbox.Changed) -> None:
        target = (event.checkbox.id or "").removeprefix("launch_")
        if target:
            self.app.handle_toggle_post_update_launch(target, event.value)


class ShortcutsTab(ScrollableContainer):
    """Content for the Shortcuts tab."""

    def compose(self) -> ComposeResult:
        with ItemGrid(id="executables_grid"):
            for runnable in shortcuts.RUNNABLES:
                yield Button(
                    runnable.label,
                    id=f"run_{runnable.key}",
                    classes="executable",
                    tooltip=runnable.tooltip,
                )
        with ItemGrid(id="folders_grid"):
            for openable in shortcuts.OPENABLES:
                if openable.css == "folder":
                    yield Button(
                        openable.label,
                        id=f"open_{openable.key}",
                        classes="folder",
                        tooltip=openable.tooltip,
                    )
        with ItemGrid(id="files_grid"):
            for openable in shortcuts.OPENABLES:
                if openable.css == "file":
                    yield Button(
                        openable.label,
                        id=f"open_{openable.key}",
                        classes="file",
                        tooltip=openable.tooltip,
                    )

    def on_mount(self) -> None:
        for attr in ("is_updating", "download_folder", "eq_path", "current_env"):
            self.watch(self.app, attr, self._recompute)

    def on_show(self) -> None:
        self._recompute()  # external installs/deletes fire no reactive; re-probe fs on show

    def _recompute(self) -> None:
        """Enable each shortcut only when its target resolves on disk."""
        self.disabled = self.app.is_updating
        for runnable in shortcuts.RUNNABLES:
            self.query_one(f"#run_{runnable.key}", Button).disabled = (
                not shortcuts.runnable_available(runnable)
            )
        for openable in shortcuts.OPENABLES:
            self.query_one(f"#open_{openable.key}", Button).disabled = (
                not shortcuts.openable_available(openable)
            )

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, ".executable")
    def handle_run_pressed(self, event: Button.Pressed) -> None:
        runnable = shortcuts.find_runnable(event.button.id.removeprefix("run_"))
        if runnable:
            self.app.run_target(runnable)

    @on(Button.Pressed, ".folder, .file")
    def handle_open_pressed(self, event: Button.Pressed) -> None:
        openable = shortcuts.find_openable(event.button.id.removeprefix("open_"))
        if openable:
            self.app.open_target(openable)


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

    def on_mount(self) -> None:
        # observe app-owned state directly
        self.watch(self.app, "username", self._refresh_account_state)
        self.watch(self.app, "is_level_2", self._refresh_account_state)

    def _refresh_account_state(self) -> None:
        app: "Redfetch" = self.app  # type: ignore[assignment]
        self.query_one("#btn_ding", Button).display = app.is_level_2 is not True
        if not app.username:
            return  # keep the "Loading..." compose default until the level check resolves
        if app.is_level_2 is True:
            text = f"[italic][bold]{app.username}, thank you for being level 2[/bold][/italic] 💛"
        elif app.is_level_2 is False:
            text = f"Hey {app.username}, you're level 1 😞 some resources won't be downloaded."
        else:
            text = f"Hey {app.username}, we couldn't verify your account level."
        self.query_one("#account_label", Label).update(text)

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, "#btn_watched")
    def handle_btn_watched_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community/watched/resources")

    @on(Button.Pressed, "#btn_account")
    def handle_btn_account_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community/amember-sso/?to=member")

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
        # Initial widget state is applied by each tab's own on_mount watch wiring (init=True).

    #
    # UI update helpers
    #

    def reset_button(self, button_id: str, variant: str = "default") -> None:
        button = self.query_one(f"#{button_id}", Button)
        button.variant = variant

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
    # single source of truth for the progress bar (and its paired resource-id input)
    progress_visible: reactive[bool] = reactive(False)
    mq_down: reactive[bool | None] = reactive(None)
    download_folder: reactive[str] = reactive("")
    eq_path: reactive[str] = reactive("")
    current_env: reactive[str] = reactive(config.settings.ENV)
    # User account identity and permissions: set reactively by background workers, observed by AccountTab for live updates
    username: reactive[str] = reactive("")
    is_level_2: reactive[bool | None] = reactive(None)
    # Startup update check: None = unknown, int = resources to be fetched
    update_count: reactive[int | None] = reactive(None)
    # Transient post-sync flash on the Easy Update button
    watched_flash: reactive[str | None] = reactive(None)

    # Post-update offer handoff between the update worker and the offer worker
    _pending_offer: post_update.PendingOffer | None = None
    # This state tracks whether an offer is actively displayed; it's reactive to trigger FetchTab updates when changed.
    _offer_active: reactive[bool] = reactive(False)

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

    def _handle_exception(self, error: Exception) -> None:
        """Show pyapp users a MessageBox when Textual crashes fatally."""
        # Textual may raise WorkerFailed wrappers; show the underlying error when available.
        root_error = getattr(error, "error", error) if isinstance(error, WorkerFailed) else error

        # Avoid repeated dialogs if follow-on exceptions happen during shutdown.
        if not self._exit and not getattr(self, "_fatal_dialog_shown", False):
            self._fatal_dialog_shown = True
            display_fatal_error(root_error)

        super()._handle_exception(error)

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
            lambda: self.action_link("https://www.redguides.com/community/amember-sso/?to=member"),
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
            lambda: self.action_link("https://www.redguides.com/community/amember-sso/?to=signup"),
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
        self.load_startup_status()
        self.check_mq_status_worker()

    def on_unmount(self) -> None:
        self.workers.cancel_all()

    #
    # Watchers
    #
    # Only current_env is watched reactively at the App (Application) level;
    # all other reactive values are watched and updated directly by the tab Views themselves
    # (using self.watch), so they remain live and responsive even when the MainScreen is not visible.

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

        # The badge count is per-env and we don't re-run the full startup check here
        self.update_count = None

        self.check_mq_status_worker()
        self.notify(f"Server type changed to: {new}")

    def watch_theme(self, theme: str) -> None:
        """Save theme preference when it changes."""
        current_theme = config.settings.get('THEME', 'textual-dark')
        if theme != current_theme:
            try:
                config.update_setting(['THEME'], theme)
            except Exception as e:
                self.notify(f"Failed to save theme preference: {e}", severity="error")

    def _get_main_screen(self) -> MainScreen | None:
        """The MainScreen only when it's the current (top) screen. Callers that push
        modals or need the active screen rely on the None-when-covered result."""
        if isinstance(self.screen, MainScreen):
            return self.screen
        return None

    def _base_main_screen(self) -> MainScreen | None:
        """The MainScreen even when a modal covers it."""
        stack = self.screen_stack
        if stack and isinstance(stack[0], MainScreen):
            return stack[0]
        return None

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
                if utils.validate_file_in_path(input_value, 'eqgame.exe'):
                    self.notify(
                        "Heads up: eqgame.exe is in this folder, which looks like your EverQuest directory. That's a bad place for downloads.",
                        severity="warning",
                    )
                self._queue_signature_reconcile()
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
                    self._queue_signature_reconcile()

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
                    if utils.validate_file_in_path(input_value, 'eqgame.exe'):
                        self.notify(
                            "Heads up: eqgame.exe is in this folder, which looks like your EverQuest directory. MacroQuest shouldn't live inside EverQuest.",
                            severity="warning",
                        )
                    self._queue_signature_reconcile()
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

    def _queue_signature_reconcile(self) -> None:
        if self.is_updating:
            return
        self.notify(f"Settings updated for {self.current_env}; changes will apply on next sync.")

    #
    # Toggle handlers
    #

    def handle_toggle_myseq(self, value: bool) -> None:
        myseq_id = utils.get_current_myseq_id()
        if myseq_id:
            current_opt_in = config.settings.from_env(self.current_env).SPECIAL_RESOURCES[myseq_id]['opt_in']
            if current_opt_in != value:
                self.update_myseq_settings(value)

    def handle_toggle_staff_picks(self, value: bool) -> None:
        """Toggle opt-in status for staff picks."""
        env = self.current_env
        pack_ids = get_staff_pick_ids_for_env(env)
        if not pack_ids:
            self.notify(f"No Staff Picks configured for {env}", severity="warning")
            return

        current_specials = config.settings.from_env(env).SPECIAL_RESOURCES

        changed = False
        for rid in pack_ids:
            current_opt_in = current_specials.get(rid, {}).get('opt_in', False)
            if current_opt_in != value:
                config.update_setting(['SPECIAL_RESOURCES', rid, 'opt_in'], value, env=env)
                changed = True

        if changed:
            state = "enabled" if value else "disabled"
            self.notify(f"Staff Picks for {env} are now {state}")

    def handle_toggle_navmesh(self, value: bool) -> None:
        current_opt_in = config.settings.from_env(self.current_env).get('NAVMESH_DOWNLOADS', None)
        # a first toggle always saves
        if current_opt_in != value:
            config.update_setting(['NAVMESH_DOWNLOADS'], value, env=self.current_env)
            state = "enabled" if value else "disabled"
            self.notify(f"navmesh downloads for {self.current_env} are now {state}")

    def handle_toggle_auto_update(self, value: bool) -> None:
        current = config.settings.from_env(self.current_env).get('AUTO_UPDATE', None)
        # a first toggle always saves
        if current != value:
            config.update_setting(['AUTO_UPDATE'], value, env=self.current_env)
            state = "enabled" if value else "disabled"
            self.notify(f"Background updates for {self.current_env} are now {state}")

    def handle_toggle_auto_run_vvmq(self, value) -> None:
        main_screen = self._get_main_screen()
        current_value = config.settings.from_env(self.current_env).get('AUTO_RUN_VVMQ', None)
        if current_value != value:
            config.update_setting(['AUTO_RUN_VVMQ'], value, env=self.current_env)
            self.notify(f"Start MQ post-update set to {tristate_label(value)}.")
        if main_screen:
            with main_screen.prevent(RadioSet.Changed):
                set_tristate(main_screen.query_one("#auto_run_vvmq", RadioSet), value)

    def handle_toggle_post_update_launch(self, target: str, enabled: bool) -> None:
        target = str(target).strip().lower()
        if not target:
            return

        targets = utils.get_post_update_targets(self.current_env)
        if enabled and target not in targets:
            targets.append(target)
        elif not enabled and target in targets:
            targets.remove(target)
        else:
            return  # already in desired state

        config.update_setting(["POST_UPDATE_LAUNCH", "targets"], targets, env=self.current_env)
        # Drop the superseded legacy single-target key if it lingers in config.
        if config.settings.from_env(self.current_env).get("POST_UPDATE_LAUNCH", {}).get("target"):
            config.update_setting(["POST_UPDATE_LAUNCH", "target"], None, env=self.current_env)

        label = utils.POST_UPDATE_PRESET_LABELS.get(target, target)
        if not enabled:
            self.notify(f"Post-update launch of {label} disabled.")
        elif target == "custom":
            self.notify(
                "Custom post-update launch is defined in settings.local.toml (see the redfetch resource for details)"
            )
        else:
            self.notify(f"Post-update launch of {label} enabled.")

        if enabled and target == "myseq":
            auto_run = config.settings.from_env(self.current_env).get("AUTO_RUN_VVMQ", None)
            if auto_run is not True:
                self.notify(
                    "RedGuides strongly recommends using MySEQ only with MQ. "
                    "Consider setting 'Start MQ post-update' to Yes.",
                    severity="warning",
                )

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
        if selected_value is None or selected_value == Select.NULL:
            brewall_opt_in = False
            good_opt_in = False
        else:
            brewall_opt_in = selected_value in ["brewall", "all"]
            good_opt_in = selected_value in ["good", "all"]

        config.update_setting(['SPECIAL_RESOURCES', '153', 'opt_in'], brewall_opt_in, env=self.current_env)
        config.update_setting(['SPECIAL_RESOURCES', '303', 'opt_in'], good_opt_in, env=self.current_env)

        if selected_value is None or selected_value == Select.NULL:
            self.notify("EQ Maps settings cleared")
        else:
            self.notify(f"EQ Maps settings updated: Brewall's Maps: {brewall_opt_in}, Good's Maps: {good_opt_in}")

    def get_current_eq_maps_value(self) -> str:
        if not self.eq_path:
            return Select.NULL
        eq_maps_status = utils.get_eq_maps_status()
        return eq_maps_status if eq_maps_status else Select.NULL

    #
    # File/folder operations
    #

    def copy_to_clipboard_with_fallback(self, text: str) -> None:
        """Textual's native copy uses OSC 52, which the legacy Windows console ignores"""
        if sys.platform != "win32":
            self.copy_to_clipboard(text)
            return
        import win32clipboard  # pywin32; Windows-only
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:
            self.notify(f"Failed to copy to clipboard: {e}", severity="error")

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

    def run_target(self, runnable) -> None:
        """Launch a shortcuts runnable, notifying on success/failure."""
        try:
            shortcuts.run(runnable)
            self.notify(f"{runnable.executable} started successfully.")
        except Exception as exc:
            message = f"Failed to start {runnable.executable}: {exc}"
            print(message)
            self.notify(message, severity="error")

    def open_target(self, openable) -> None:
        """Open a shortcuts folder/file. Folders open visibly; files get a toast."""
        try:
            detail = shortcuts.open_target(openable)
        except Exception as exc:
            self.notify(f"Couldn't open {openable.label}: {exc}", severity="error")
            return
        if openable.filename is not None:
            self.notify(f"{openable.filename} opened{(' ' + detail) if detail else ''}.")

    def run_command(self, command, cwd=None) -> None:
        """Run a command and notify on success or failure."""
        label = os.path.basename(utils._command_program(command)) or "post-update program"
        try:
            processes.run_command(command, cwd)
            self.notify(f"{label} started successfully.")
        except Exception as exc:
            message = f"Failed to start {label}: {exc}"
            print(message)
            self.notify(message, severity="error")

    def handle_uninstall(self) -> None:
        """Handle the uninstall button press."""
        def handle_uninstall_response(response: str) -> None:
            if response == UninstallScreen.RESPONSE_YES:
                try:
                    with self.suspend():
                        meta.uninstall()
                except SystemExit:
                    print("bye bye!")
                    self.exit()
            else:
                # username is always a reactive
                username = self.username or "You"
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
            if worker.name == "_update_watched_worker":
                # consume here, not in update_complete: a covering screen would drop the offer
                pending, self._pending_offer = self._pending_offer, None
                if main_screen:
                    self.update_complete(worker.result, main_screen.query_one("#update_watched", Button))
                # Use pending.decision (not worker.result)
         
                if pending is not None and pending.decision is not post_update.Decision.NONE:
                    self._offer_active = True  # holds is_updating until the offer worker finishes
                    self._post_update_worker(pending)
                elif worker.result:
                    self.set_timer(6, self._clear_watched_flash)
            elif worker.name == "_update_single_resource_worker" and main_screen:
                self.update_complete(worker.result, main_screen.query_one("#update_resource_id", Button))
            elif worker.name == "_redguides_interface_worker":
                self.notify("RedGuides Interface is now running.")

        elif state == WorkerState.ERROR:
            error_message = f"Worker {worker.name} encountered an error: {worker.error}"
            self.notify(error_message, severity="error")
            print(error_message)

            if worker.name == "_update_watched_worker":
                self.watched_flash = "error"
            elif worker.name == "_update_single_resource_worker" and main_screen:
                main_screen.query_one("#update_resource_id", Button).variant = "error"

        elif state == WorkerState.CANCELLED:
            self.notify(f"Worker {worker.name} was cancelled.", severity="warning")

        if group in {"update_watched_group", "single_update_group", "maintenance_group"}:
            if state in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
                # hide the bar on any terminal state — ERROR skips update_complete entirely
                self.progress_visible = False
                # a dispatched offer keeps the gate until the offer worker finishes
                if not self._offer_active:
                    self.is_updating = False
        elif group == "post_update_group":
            if state in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
                self._offer_active = False
                self.is_updating = False

        if group == "interface_group":
            if worker.name == "_redguides_interface_worker":
                if state in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
                    self.interface_running = False
            elif worker.name == "_prepare_redguides_interface_worker":
                if state in {WorkerState.ERROR, WorkerState.CANCELLED}:
                    self.interface_running = False

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
    async def _update_watched_worker(self) -> SyncOutcome:
        print("Starting update of all watched & special resources, please wait...")

        outcome = await self.run_synchronization()
        # scan + decide now, while is_updating still gates env switching and re-clicks
        self._pending_offer = await post_update.prepare(outcome)
        return outcome

    def cancel_update_watched(self):
        cancelled_workers = self.workers.cancel_group(self, "update_watched_group")
        if cancelled_workers:
            self.notify("Update canceled.", severity="warning")

    def on_sync_event(self, event: SyncEvent) -> None:
        """Handle events from the sync process to update the UI."""
        event_type, resource_id, details = event
        self._process_sync_event(event_type, resource_id, details)

    def _process_sync_event(self, event_type: str, resource_id: str | int, details: str | None) -> None:
        """Process sync events on the main thread."""
        # _base_main_screen so the bar advances
        main_screen = self._base_main_screen()
        try:
            if event_type == "total":
                total_tasks = int(resource_id)
                if total_tasks > 0:
                    # visibility is state-derived; _update_from_state pairs the bar with the input
                    self.progress_visible = True
                    if main_screen:
                        progress_bar = main_screen.query_one(FetchTab).query_one("#update_progress", ProgressBar)
                        progress_bar.total = total_tasks
                        progress_bar.progress = 0
            elif event_type == "add_total" and main_screen:
                # Extend total (e.g., for navmesh phase)
                additional = int(resource_id)
                if additional > 0:
                    progress_bar = main_screen.query_one(FetchTab).query_one("#update_progress", ProgressBar)
                    progress_bar.total = (progress_bar.total or 0) + additional
            elif event_type == "done" and main_screen:
                main_screen.query_one(FetchTab).query_one("#update_progress", ProgressBar).advance(1)
        except Exception:
            pass

    async def run_synchronization(self, resource_ids=None) -> SyncOutcome:
        try:
            db_name = f"{self.current_env}_resources.db"
            await asyncio.to_thread(store.initialize_db, db_name)
            db_path = store.get_db_path(db_name)
            headers = await auth.get_api_headers()
            if resource_ids:
                reset_success = await asyncio.to_thread(
                    store.reset_download_dates_for_resources, db_name, resource_ids
                )
                if not reset_success:
                    return SyncOutcome(success=False)
            result = await sync.run_sync(
                db_path, headers,
                resource_ids=resource_ids,
                on_event=self.on_sync_event,
            )
            return result
        except Exception:
            traceback.print_exc()
            return SyncOutcome(success=False)

    @work(group="post_update_group", exclusive=True)
    async def _post_update_worker(self, pending: post_update.PendingOffer) -> None:
        try:
            await post_update.execute(pending, _TuiPostUpdate(self))
        finally:
            self.set_timer(6, self._clear_watched_flash)

    def _clear_watched_flash(self) -> None:
        """Drop the post-sync flash."""
        self.watched_flash = None

    def update_complete(self, result, button: Button) -> None:
        # bar/input visibility follows progress_visible, cleared on worker completion below
        main_screen = self._get_main_screen()
        status = getattr(result, "status", "ok" if result else "failed")
        is_watched = button.id == "update_watched"
        if result:
            self.notify("All resources updated successfully.")
            if is_watched:
                self.update_count = 0  # everything watched is now fetched — clear the badge
                self.watched_flash = "success"
            else:
                button.variant = "success"
            if button.id == "update_resource_id" and main_screen:
                input_widget = main_screen.query_one("#resource_id_input", Input)
                input_widget.value = ""
                self.set_timer(6, lambda: main_screen.reset_button("update_resource_id", "default"))
        elif status in ("busy", "cancelled"):
            # not a failure: a peer holds the update lock, or the user stopped it.
            if is_watched:
                self.watched_flash = None  # settle to the count-derived resting variant
            else:
                button.variant = "primary"
            if status == "busy":
                self.notify("Another update is already in progress; try again shortly.", severity="warning")
            if button.id == "update_resource_id" and main_screen:
                main_screen.query_one("#resource_id_input", Input).value = ""
        else:
            if is_watched:
                self.watched_flash = "error"
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
    async def _update_single_resource_worker(self, resource_id: str) -> SyncOutcome:
        result = await self.run_synchronization([resource_id])
        return result

    def cancel_redguides_interface(self):
        self.workers.cancel_group(self, "interface_group")
    
    def handle_toggle_desktop_shortcut(self, value: bool) -> None:
        """Ensure the Desktop shortcut is enabled/disabled (Windows-only)."""
        if sys.platform != "win32":
            self.notify("Desktop shortcuts are only supported on Windows.", severity="warning")
            return

        if value:
            shortcut_path = desktop_shortcut.create_shortcut()
            self.notify(f"Desktop shortcut created: {shortcut_path}")
        else:
            desktop_shortcut.remove_shortcut()
            self.notify("Desktop shortcut removed.")
        # The switch already reflects the user's toggle; SettingsTab.on_show re-probes the fs.

    def handle_reset_downloads(self) -> None:
        if self.is_updating:
            return
        self.notify("Resetting all download dates...")
        self.is_updating = True
        self._reset_downloads_worker()

    @work(exclusive=True, group="maintenance_group")
    async def _reset_downloads_worker(self) -> bool:
        try:
            print("Resetting all download dates")
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
        db_name = f"{self.current_env}_resources.db"
        await asyncio.to_thread(store.initialize_db, db_name)
        headers = await auth.get_api_headers()
        settings = config.settings.from_env(self.current_env)
        category_map = config.CATEGORY_MAP
        self._redguides_interface_worker(
            settings,
            db_name,
            headers,
            category_map,
        )
        return True

    @work(exclusive=True, group="interface_group")
    async def _redguides_interface_worker(self, settings, db_name, headers, category_map) -> bool:
        from redfetch.listener import run_server_async
        await run_server_async(settings, db_name, headers, category_map)
        return True
    
    @work
    async def load_startup_status(self):
        """Set the account level, the update badge, and print an update summary at startup."""
        try:
            username = await auth.get_username()
        except RuntimeError:
            print("Couldn't verify your RedGuides account right now.")
            return

        try:
            headers = await auth.get_api_headers()
        except RuntimeError:
            # Token expired mid-session — unknown, not "level 1": keep identity, leave is_level_2.
            self.username = username
            return

        try:
            db_name = f"{self.current_env}_resources.db"
            await asyncio.to_thread(store.initialize_db, db_name)
            prepared = await sync.prepare_sync(store.get_db_path(db_name), headers)
        except Exception:
            self.username = username
            print("Couldn't check for updates right now.")
            return

        # is_level_2 first so the username-triggered recompute sees the resolved level.
        self.is_level_2 = prepared.sync_info.is_level_2
        self.username = username

        self.update_count, summary = _startup_update_summary(prepared.execution_plan)
        print(summary)

    def handle_ding_check(self) -> None:
        """Check if user has upgraded to level 2 and update UI accordingly."""
        self.notify("Checking your level... 🎲")
        self._check_ding_level_worker()

    @work(exclusive=True, group="ding_check_group")
    async def _check_ding_level_worker(self) -> None:
        """Worker to check level 2 status and update UI or redirect."""
        # Dev-only crash injection to verify pyapp crash dialog behavior.
        if os.environ.get("REDFETCH_CRASH_TEST") == "ding":
            raise RuntimeError("Intentional crash test from _check_ding_level_worker.")

        try:
            headers = await auth.get_api_headers()
            sync_info = await api.get_sync_info(headers)
        except RuntimeError as exc:
            # Auth is unrecoverable this session (e.g. token refresh failed).
            self.notify(str(exc), severity="error", timeout=10)
            return
        except (httpx.HTTPStatusError, httpx.RequestError):
            self.notify(
                "Couldn't check your account level right now.",
                severity="error",
                timeout=10,
            )
            return

        if sync_info.is_level_2:
            # User is now level 2! AccountTab + FetchTab react to the reactives below.
            self.username = self.username or await auth.get_username()
            self.is_level_2 = True
            self.notify("🎉 DING! Welcome to level 2!", severity="information")
        else:
            # Still level 1, send them to the signup page
            self.notify("You're still level 1. Opening upgrade page...", severity="warning")
            self.action_link("https://www.redguides.com/community/amember-sso/?to=signup")


# display print statements in the log widget
class PrintCapturingLog(Log):
    def on_mount(self) -> None:
        self.begin_capture_print()

    def on_print(self, event: Print) -> None:
        self.write(event.text)


class _TuiPostUpdate:
    """TUI adapter for post_update.execute: modals + notify. Runs inside a worker."""

    def __init__(self, app) -> None:
        self.app = app

    def notify(self, message: str, *, error: bool = False) -> None:
        print(message)  # PrintCapturingLog echoes it in the terminal widget
        self.app.notify(message, severity="error" if error else "information")

    async def confirm_restart(self) -> bool:
        response = await self.app.push_screen_wait(RunVVMQScreen(post_update.Decision.RESTART))
        return response == RunVVMQScreen.RESPONSE_RUN

    async def ask_cold_start(self) -> post_update.ColdStartChoice:
        response = await self.app.push_screen_wait(RunVVMQScreen(post_update.Decision.COLD_START))
        return {
            RunVVMQScreen.RESPONSE_RUN: "yes",
            RunVVMQScreen.RESPONSE_ALWAYS: "always",
            RunVVMQScreen.RESPONSE_NEVER: "never",
        }.get(response, "no")

    def auto_run_persisted(self, value: bool) -> None:
        # config already written; the handler skips the no-op write and syncs the radio set
        self.app.handle_toggle_auto_run_vvmq(value)

    async def wait_for_eq_close(self) -> bool:
        return await self.app.push_screen_wait(CloseEQScreen())


class CloseEQScreen(ModalScreen[bool]):
    """Wait for the user to close EverQuest; dismisses True once it's gone, False on Cancel."""

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(
                "Close EverQuest to finish restarting MacroQuest.\n"
                "This closes automatically once EverQuest has exited.",
                id="question",
            ),
            Center(Button("Cancel", variant="default", id="canceleq")),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.set_interval(1.0, self._check_closed)

    async def _check_closed(self) -> None:
        # Cancel may have dismissed mid-poll; dismissing an inactive screen raises
        if not await asyncio.to_thread(processes.get_eqgame_process_pids):
            if self.is_current:
                self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.is_current:
            self.dismiss(False)


class RunVVMQScreen(ModalScreen):
    """A modal screen to ask if the user wants to run (or restart) Very Vanilla MQ."""

    RESPONSE_RUN = "run"
    RESPONSE_ALWAYS = "always"
    RESPONSE_NEVER = "never"
    RESPONSE_SKIP = "skip"

    def __init__(self, decision=None):
        super().__init__()
        self._decision = decision

    def compose(self) -> ComposeResult:
        restart = self._decision is post_update.Decision.RESTART
        widgets = [
            Label("Restart Very Vanilla MQ?" if restart else "Run Very Vanilla MQ?", id="question"),
            Button("Yes", variant="primary", id="yesmq"),
            Button("No", variant="default", id="nomq"),
        ]
        if not restart:
            # Always/Never persist AUTO_RUN_VVMQ, which governs cold starts only
            widgets.append(Center(Button("Always", variant="primary", id="alwaysmq")))
            widgets.append(Center(Button("Never", variant="default", id="nevermq")))
        yield Grid(*widgets, id="dialog", classes="two_row" if restart else "")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yesmq":
            self.dismiss(self.RESPONSE_RUN)
        elif event.button.id == "alwaysmq":
            self.dismiss(self.RESPONSE_ALWAYS)
        elif event.button.id == "nevermq":
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
