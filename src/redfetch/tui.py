# standard
import asyncio
import os
import sys
import threading
import traceback
import webbrowser
from contextlib import suppress
from pathlib import Path
from itertools import cycle

# third-party
import httpx
from dynaconf import ValidationError
from textual_fspicker import FileOpen, SelectDirectory

# textual framework
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.widgets import Footer, Button, Header, Input, Select, TabbedContent, TabPane, Log, ProgressBar, RadioSet
from textual.reactive import reactive
from textual.worker import Worker, WorkerState, WorkerFailed, get_current_worker
from textual.screen import Screen

# local
from redfetch import store
from redfetch import api
from redfetch import auth
from redfetch import config
from redfetch import net
from redfetch import patcher
from redfetch import post_update
from redfetch import provision
from redfetch import utils
from redfetch import meta
from redfetch import servers
from redfetch import sync
from redfetch import shortcuts
from redfetch import desktop_shortcut
from redfetch import laa
from redfetch.sync_types import ExecutionPlan, SyncEvent, SyncOutcome
from redfetch.runtime_errors import display_fatal_error
from redfetch.tui_account import AccountTab
from redfetch.tui_fetch import FetchTab
from redfetch.tui_modals import UninstallScreen, _TuiPostUpdate
from redfetch.tui_servers import ProvisionProgress, ServersTab
from redfetch.tui_settings import SettingsTab, get_staff_pick_ids_for_env
from redfetch.tui_shortcuts import ShortcutsTab
from redfetch.tui_widgets import (
    BARE_SETUP_ID, clean_source_filters, set_tristate, tristate_label,
)

# for dev mode, from root dir:
# "hatch shell dev"
# "textual run --dev .\src\redfetch\main.py"


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

            with TabPane("Servers", id="servers"):
                yield ServersTab(id="servers_scroll")

            with TabPane("Shortcuts", id="shortcuts"):
                yield ShortcutsTab(id="shortcuts_scroll")

            with TabPane("Account", id="account"):
                yield AccountTab(id="account_grid")

    def on_mount(self) -> None:
        """Initialize the screen after widgets are mounted."""
        # Initialize the Log widget with some content
        log = self.query_one("#fetch_log", Log)
        log.write_line(f"redfetch v{meta.get_current_version()} allows you to download resources from RedGuides, and more.")
        log.write_line("It is not affiliated with or endorsed by EverQuest or its owners.")
        env = self.app.current_env
        log.write_line("Client: " + config.ENVS[env])
        if servers.is_multi_server(env):
            active = self.app.active_server
            log.write_line(
                "Server: "
                + (servers.server_label(active, env) if active else config.BARE_SERVER_LABEL)
            )
        log.write_line("\n")

        # Set border titles (settings tab handles its own dynamic titles)
        self.query_one("#client_select_fetch").border_title = "Client"
        self.query_one("#server_select_fetch").border_title = "Server"
        self.query_one("#executables_grid").border_title = "Executables ⚡"
        self.query_one("#folders_grid").border_title = "Folders 📁"
        self.query_one("#files_grid").border_title = "Files 📎"
        self.query_one("#server_list").border_title = "Emu servers"
        # The environment watcher does not run at mount.
        self.app.apply_servers_tab_visibility()
        # Initial widget state is applied by each tab's own on_mount watch wiring (init=True).

    #
    # UI update helpers
    #

    def reset_button(self, button_id: str, variant: str = "default") -> None:
        button = self.query_one(f"#{button_id}", Button)
        button.variant = variant


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
    # Active server slug on a multi-server env, else None.
    active_server: reactive[str | None] = reactive(None)
    # User account identity and permissions: set reactively by background workers, observed by AccountTab for live updates
    username: reactive[str] = reactive("")
    is_level_2: reactive[bool | None] = reactive(None)
    # Startup update check: None = unknown, int = resources to be fetched
    update_count: reactive[int | None] = reactive(None)
    # Transient post-sync flash on the Easy Update button
    watched_flash: reactive[str | None] = reactive(None)

    # A second "Get patcher" press must not cancel-and-restart the download.
    patcher_install_running: bool = False

    # A second "Allow 4GB Memory" press must not stack a duplicate rewrite.
    laa_enable_running: bool = False

    # Aa provision locks down every tab
    provision_running: reactive[bool] = reactive(False)
    # False once the folder has landed
    provision_cancellable: bool = False
    _provision_cancel: threading.Event | None = None

    # Post-update offer handoff between the update worker and the offer worker
    _pending_offer: post_update.PendingOffer | None = None
    # This state tracks whether an offer is actively displayed; it's reactive to trigger FetchTab updates when changed.
    _offer_active: reactive[bool] = reactive(False)

    CSS_PATH = "tui.tcss"

    MODES = {"main": MainScreen}

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+t", "cycle_theme", "Theme"),
        ("ctrl+f", "focus_search", "Search Log"),
        ("ctrl+s", "cycle_env", "Client"),
        Binding("ctrl+r", "start_interface", "RG.com Interface", tooltip="Download resources while you browse redguides.com"),
        Binding("ctrl+r", "stop_interface", "Stop Interface", tooltip="Other buttons are disabled until you stop the interface"),
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
        self.active_server = (
            servers.get_active_server(self.current_env)
            if servers.is_multi_server(self.current_env) else None
        )

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
    # Only current_env is watched at the App level. The other reactives are
    # watched by their own tab Views, so they stay live even when MainScreen isn't showing.

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

        self.active_server = (
            servers.get_active_server(new) if servers.is_multi_server(new) else None
        )
        self.apply_servers_tab_visibility()

        # Apply theme for new environment
        new_theme = settings_for_env.get('THEME', 'textual-dark')
        self.theme = new_theme

        # The badge count is per-env and we don't re-run the full startup check here
        self.update_count = None

        self.check_mq_status_worker()
        self.notify(f"Client: {config.ENVS[new]}")

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

    def action_cycle_env(self) -> None:
        """Cycle to the next client; its active server resumes as-is."""
        if self.is_updating or self.interface_running or self.provision_running:
            return

        order = tuple(config.ENVS)
        try:
            index = order.index(self.current_env)
        except ValueError:
            index = 0
        new_env = order[(index + 1) % len(order)]
        self.current_env = new_env

    def action_focus_search(self) -> None:
        """Focus the log search input."""
        main_screen = self._get_main_screen()
        if not main_screen:
            return
        with suppress(Exception):
            search_input = main_screen.query_one("#log_search", Input)
            tabbed_content = main_screen.query_one(TabbedContent)
            if tabbed_content.active != "fetch":
                tabbed_content.active = "fetch"
            search_input.focus()

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
            # Hide when running, and while a provision owns the app
            return not self.interface_running and not self.provision_running
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
        elif input_id in ("eq_path_input", "server_eq_path_input"):
            # One branch for both copies of the setting: Settings tab and Servers tab.
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
        elif input_id == "clean_source_input":
            try:
                provision.set_clean_source(input_value)
            except provision.ProvisionError as exc:
                # Names a folder that's already serving a server.
                self.notify(str(exc), severity="error", markup=False)
                return
            self.notify(
                "Clean RoF2 copy updated" if input_value else "Clean RoF2 copy cleared"
            )
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

    def select_clean_source(self, input_id: str) -> None:
        """Pick a clean RoF2 archive. A folder source is typed, or picked in the Add dialog."""
        main_screen = self._get_main_screen()
        if not main_screen:
            return
        input_widget = main_screen.query_one(f"#{input_id}")
        start = utils.nearest_dir(input_widget.value.strip())

        def picked(path: Path | None) -> None:
            if not path:
                return
            input_widget.value = str(path)
            self.handle_input_update(input_id, str(path))

        self.push_screen(
            FileOpen(location=start, filters=clean_source_filters()), callback=picked
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
        self.notify(f"Settings updated for {config.ENVS[self.current_env]}; changes will apply on next sync.")

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
            self.notify(f"No Staff Picks configured for {config.ENVS[env]}", severity="warning")
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
            self.notify(f"Staff Picks for {config.ENVS[env]} are now {state}")

    def handle_toggle_navmesh(self, value: bool) -> None:
        current_opt_in = config.settings.from_env(self.current_env).get('NAVMESH_DOWNLOADS', None)
        # a first toggle always saves
        if current_opt_in != value:
            config.update_setting(['NAVMESH_DOWNLOADS'], value, env=self.current_env)
            state = "enabled" if value else "disabled"
            self.notify(f"navmesh downloads for {config.ENVS[self.current_env]} are now {state}")

    def handle_toggle_auto_update(self, value: bool) -> None:
        current = config.settings.from_env(self.current_env).get('AUTO_UPDATE', None)
        # a first toggle always saves
        if current != value:
            config.update_setting(['AUTO_UPDATE'], value, env=self.current_env)
            state = "enabled" if value else "disabled"
            self.notify(f"Background updates for {config.ENVS[self.current_env]} are now {state}")

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
            self.notify(f"MySEQ for {config.ENVS[self.current_env]} is now {state}")
        else:
            self.notify("MySEQ is not available for this client", severity="error")

    def update_eq_maps_settings(self, selected_value: str | None) -> None:
        # None/NULL fall out on their own
        brewall_opt_in = selected_value in ("brewall", "all")
        good_opt_in = selected_value in ("good", "all")

        config.update_setting(['SPECIAL_RESOURCES', config.MAPS_MAP["brewall"], 'opt_in'], brewall_opt_in, env=self.current_env)
        config.update_setting(['SPECIAL_RESOURCES', config.MAPS_MAP["good"], 'opt_in'], good_opt_in, env=self.current_env)

        if selected_value is None or selected_value == Select.NULL:
            self.notify("EQ Maps settings cleared")
        else:
            self.notify(f"EQ Maps settings updated: Brewall's Maps: {brewall_opt_in}, Good's Maps: {good_opt_in}")

    def get_current_eq_maps_value(self) -> str:
        if not self.eq_path:
            return Select.NULL
        return utils.get_eq_maps_status() or Select.NULL

    #
    # Server handling (emu multipath)
    #

    def apply_servers_tab_visibility(self) -> None:
        """Show the Servers tab only for multi-server clients."""
        main_screen = self._base_main_screen()
        if not main_screen:
            return
        tabbed = main_screen.query_one(TabbedContent)
        if servers.is_multi_server(self.current_env):
            tabbed.show_tab("servers")
        else:
            tabbed.hide_tab("servers")

    def refresh_after_server_change(self) -> None:
        """Refresh tabs even when a server switch leaves watched values unchanged."""
        settings_for_env = config.settings.from_env(self.current_env)
        self.eq_path = settings_for_env.EQPATH or ""
        self.download_folder = utils.get_current_download_folder()
        self.active_server = (
            servers.get_active_server(self.current_env)
            if servers.is_multi_server(self.current_env) else None
        )
        self.update_count = None  # the plan behind the badge changed
        main_screen = self._base_main_screen()
        if main_screen:
            main_screen.query_one(FetchTab)._recompute()
            main_screen.query_one(SettingsTab)._recompute()
            main_screen.query_one(ShortcutsTab)._recompute()
            main_screen.query_one(ServersTab)._recompute()

    def switch_active_server(self, slug: str) -> None:
        if slug == servers.get_active_server(self.current_env):
            # Ignore mount-time events for the already-active server.
            return
        if self.is_updating or self.interface_running or self.provision_running:
            return
        try:
            notices = servers.switch_server(slug)
        except (servers.ServerSwitchError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        for notice in notices or []:
            self.notify(notice, severity="warning")
        self.refresh_after_server_change()
        label = servers.server_label(slug, self.current_env)
        self.notify(f"Server: {label}", markup=False)  # labels are free text

    def switch_to_bare_setup(self, env: str) -> None:
        """Leave the active server for the client's own setup."""
        if self.is_updating or self.interface_running or self.provision_running:
            return
        try:
            notices = servers.switch_to_generic(env)
        except (servers.ServerSwitchError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        for notice in notices or []:
            self.notify(notice, severity="warning")
        self.refresh_after_server_change()
        self.notify(f"Server: {config.BARE_SERVER_LABEL}")

    def install_active_patcher(self) -> bool:
        """Fetch the active server's own patcher"""
        # laa_enable_running too: both rewrite eqgame.exe, and the payload move must not race it.
        if (self.is_updating or self.interface_running or self.patcher_install_running
                or self.laa_enable_running or self.provision_running):
            return False
        context = servers.active_server_context(self.current_env)
        if not patcher.has_patcher(context):
            return False
        self.patcher_install_running = True
        self.notify(f"Downloading the {context.label} patcher...", markup=False)
        self._install_patcher_worker(context)
        return True

    @work(exclusive=True, group="patcher_group")
    async def _install_patcher_worker(self, context: servers.ServerContext) -> bool:
        installed = False
        try:
            await patcher.install(context)
        except patcher.PatcherError as exc:
            # The message interpolates a free-text server label.
            self.notify(str(exc), severity="error", markup=False)
        except Exception as exc:
            print(f"Error in _install_patcher_worker: {exc}")
            self.notify("Couldn't download the patcher.", severity="error")
        else:
            installed = True
            self.notify(
                f"The {context.label} patcher is installed in its EverQuest folder.",
                markup=False,
            )
        finally:
            self.patcher_install_running = False
        main_screen = self._base_main_screen()
        if main_screen:
            # Both the button and the shortcut key off the exe being on disk.
            main_screen.query_one(ServersTab)._recompute()
            main_screen.query_one(ShortcutsTab)._recompute()
        return installed

    def enable_active_laa(self) -> bool:
        """Set the 4GB flag on the active server's eqgame.exe."""
        if (self.is_updating or self.interface_running or self.patcher_install_running
                or self.laa_enable_running or self.provision_running):
            return False
        context = servers.active_server_context(self.current_env)
        if laa.status(context.eqpath).state is not laa.LaaState.OFF:
            return False
        self.laa_enable_running = True
        self._enable_laa_worker(context)
        return True

    @work(exclusive=True, group="laa_group")
    async def _enable_laa_worker(self, context: servers.ServerContext) -> bool:
        enabled = False
        try:
            # Cancelling this worker mid-write is safe. enable() cleans up.
            await asyncio.to_thread(laa.enable, context.eqpath)
        except laa.LaaError as exc:
            # markup=False: the error text may contain a folder path with [brackets].
            self.notify(str(exc), severity="error", markup=False)
        except Exception as exc:
            print(f"Error in _enable_laa_worker: {exc}")
            self.notify("Couldn't update eqgame.exe.", severity="error")
        else:
            enabled = True
            self.notify(
                f"eqgame.exe can now use up to 4GB of memory. "
                f"The original is kept as {laa.BACKUP_NAME}."
            )
        finally:
            self.laa_enable_running = False
        main_screen = self._base_main_screen()
        if main_screen:
            main_screen.query_one(ServersTab)._recompute()
        return enabled

    def provision_server(self, payload: dict, *, switch_after: bool = False) -> bool:
        """Create a server's EverQuest folder from the clean RoF2 copy, then set it up."""
        if (self.is_updating or self.interface_running or self.patcher_install_running
                or self.laa_enable_running or self.provision_running):
            # The tab's own _busy() misses the patcher and LAA runs, so we need this.
            self.notify("redfetch is busy; try again when it finishes.", severity="warning")
            return False
        env = self.current_env
        slug = payload["slug"]
        try:
            # Another terminal's CLI can claim the slug while the dialog sits open.
            if servers.is_server_configured(slug, env):
                raise ValueError(
                    f"{servers.server_label(slug, env)} already has an EverQuest folder."
                )
            if slug not in servers.list_servers(env):
                servers.validate_server_slug(slug, must_be_new=True)
        except ValueError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return False
        self._provision_cancel = threading.Event()
        # Set before provision_running or the Cancel button stays disabled.
        self.provision_cancellable = True
        self.provision_running = True
        self._provision_worker(payload, env=env, switch_after=switch_after)
        return True

    @work(exclusive=True, group="provision_group")
    async def _provision_worker(self, payload: dict, *, env: str, switch_after: bool) -> bool:
        worker = get_current_worker()
        stop = self._provision_cancel
        slug = payload["slug"]
        created = False

        def report(label: str, fraction: float | None) -> None:
            self.post_message(ProvisionProgress(label, fraction))

        try:
            result = await provision.provision(
                slug,
                env=env,
                source=payload["source"],
                destination=payload["destination"],
                label=payload.get("label") or "",
                patcher_url=payload.get("patcher_url") or "",
                patcher_exe=payload.get("patcher_exe") or "",
                progress=report,
                # Quitting cancels every worker; the copy thread has to hear about it too.
                cancelled=lambda: stop.is_set() or worker.is_cancelled,
            )
        except provision.ProvisionCancelled as exc:
            # Subclasses ProvisionError, so it has to be caught first.
            self.notify(str(exc), severity="warning", markup=False)
        except provision.ProvisionError as exc:
            self.notify(str(exc), severity="error", markup=False)
        except ValueError as exc:
            # add_server refused after the folder landed: it's real, just not registered.
            self.notify(
                f"{payload['destination']} was created, but adding the server failed: "
                f'{exc} Add it with "use an existing folder".',
                severity="error", markup=False,
            )
        else:
            created = True
            for notice in result.notices:
                self.notify(notice, severity="warning", markup=False)
            self.notify(f"{servers.server_label(slug, env)} is ready to play.", markup=False)
        finally:
            self.provision_running = False
            self.provision_cancellable = False
            self.refresh_after_server_change()
        if created and switch_after:
            # Must wait until provision_running is cleared.            
            self.switch_active_server(slug)
        return created

    def cancel_provision(self) -> None:
        """Stop the copy. Nothing is left behind."""
        if not self.provision_running:
            return
        if not self.provision_cancellable:
            self.notify("Too late to cancel — finishing setup.", severity="warning")
            return
        self._provision_cancel.set()
        self.notify("Cancelling...")

    def on_provision_progress(self, message: ProvisionProgress) -> None:
        if message.label == provision.FINISHING_LABEL:
            self.provision_cancellable = False
        main_screen = self._base_main_screen()
        if main_screen:
            main_screen.query_one(ServersTab).show_provision_progress(
                message.label, message.fraction
            )

    def handle_client_selected(self, token: str) -> None:
        """Switch clients; the incoming client's active server resumes untouched."""
        if token != self.current_env:
            self.current_env = token  # watch_current_env does the switch

    def handle_server_selected(self, value: str) -> None:
        """Switch to the server the user picked from the dropdown, or leave the current server if they picked the bare setup."""
        env = self.current_env
        if value == BARE_SETUP_ID:
            active = servers.get_active_server(env)
            if active is not None and servers.is_server_configured(active, env):
                self.switch_to_bare_setup(env)
                if servers.get_active_server(env):
                    # Blocked: put the dropdown back on the server we're still on.
                    self.refresh_after_server_change()
            return
        self.switch_active_server(value)
        if self.active_server != value:
            # Restore the dropdowns if the switch was blocked.
            self.refresh_after_server_change()

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

    STARTUP_GROUP = "startup_group"

    def run_target(self, runnable) -> None:
        """Launch a shortcuts runnable, notifying on success/failure."""
        if runnable.startup:
            # Cancelling a to_thread worker does not stop its thread.
            if any(
                w.group == self.STARTUP_GROUP and w.state in (WorkerState.PENDING, WorkerState.RUNNING)
                for w in self.workers
            ):
                self.notify(f"{runnable.label} is already starting.")
                return
            self._startup_worker(runnable)
            return
        try:
            shortcuts.run(runnable)
            # Free-text exe names (e.g. custom servers) may contain [brackets]; skip markup.
            self.notify(f"{shortcuts.runnable_executable(runnable)} started successfully.", markup=False)
        except Exception as exc:
            # Use the label, not the executable: a failed resolve has no exe to name.
            message = f"Failed to start {shortcuts.runnable_label(runnable)}: {exc}"
            print(message)
            self.notify(message, severity="error", markup=False)

    @work(group=STARTUP_GROUP)
    async def _startup_worker(self, runnable) -> None:
        try:
            result = await asyncio.to_thread(runnable.startup)
        except Exception as exc:
            message = f"Failed to start {runnable.label}: {exc}"
            print(message)
            self.notify(message, severity="error", markup=False)
            return
        for message, is_error in result.messages:
            print(message)
            self.notify(message, severity="error" if is_error else "information", markup=False)

    def open_target(self, openable) -> None:
        """Open a shortcuts folder/file. Folders open visibly; files get a toast."""
        try:
            detail = shortcuts.open_target(openable)
        except Exception as exc:
            self.notify(f"Couldn't open {openable.label}: {exc}", severity="error")
            return
        if openable.filename is not None:
            self.notify(f"{openable.filename} opened{(' ' + detail) if detail else ''}.")

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
        if self.is_updating or self.provision_running:
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
        event_type, resource_id, _details = event
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
            pass  # progress display is nice but never let it break the sync

    async def run_synchronization(self, resource_ids=None) -> SyncOutcome:
        try:
            db_name = store.db_name(self.current_env)
            await asyncio.to_thread(store.initialize_db, db_name)
            db_path = store.get_db_path(db_name)
            headers = await auth.get_api_headers()
            if resource_ids:
                reset_success = await asyncio.to_thread(
                    store.reset_download_dates_for_resources, db_name, resource_ids,
                    servers.active_server_slug(self.current_env),
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
        # Can be invoked via palette even when the button is disabled.
        if self.is_updating or self.provision_running or not main_screen:
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
        if self.is_updating or self.provision_running:
            return
        self.notify("Resetting all download dates...")
        self.is_updating = True
        self._reset_downloads_worker()

    @work(exclusive=True, group="maintenance_group")
    async def _reset_downloads_worker(self) -> bool:
        try:
            print("Resetting all download dates")
            db_name = store.db_name(self.current_env)
            db_path = store.get_db_path(db_name)
            await store.reset_download_dates_async(db_path)
            self.notify("All download dates have been reset successfully.")
            return True
        except Exception as e:
            print(f"Error in _reset_downloads_worker: {e}")
            self.notify("Failed to reset download dates.", severity="error")
            return False

    def handle_redguides_interface(self) -> None:
        if self.provision_running:
            # Reachable via ctrl+r and palette even while widgets are disabled.
            self.notify("Wait for the server setup to finish.", severity="warning")
            return
        self.interface_running = True
        self.notify("Starting RedGuides Interface...")
        self._prepare_redguides_interface_worker()

    @work(exclusive=True, group="interface_group")
    async def _prepare_redguides_interface_worker(self) -> bool:
        db_name = store.db_name(self.current_env)
        await asyncio.to_thread(store.initialize_db, db_name)
        headers = await auth.get_api_headers()
        category_map = config.CATEGORY_MAP
        self._redguides_interface_worker(
            db_name,
            headers,
            category_map,
        )
        return True

    @work(exclusive=True, group="interface_group")
    async def _redguides_interface_worker(self, db_name, headers, category_map) -> bool:
        from redfetch.listener import run_server_async
        await run_server_async(db_name, headers, category_map)
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
            db_name = store.db_name(self.current_env)
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


def run_textual_ui():
    app = Redfetch()
    app.run()


if __name__ == "__main__":
    run_textual_ui()
