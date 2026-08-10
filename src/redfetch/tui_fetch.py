"""The Fetch tab: the update button, the single-resource input, and the log."""
# standard
from typing import NamedTuple

# third-party
from rich.console import detect_legacy_windows

# textual framework
from textual import on
from textual.app import ComposeResult
from textual.containers import Center, CenterMiddle, Grid, ScrollableContainer, Vertical
from textual.events import Print
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, Input, Label, Log, ProgressBar, Select

# local
from redfetch.tui_widgets import (
    ServerSelect, make_client_select, server_select_state, sync_client_select,
    sync_server_select,
)


class WatchedButtonState(NamedTuple):
    label: str
    tooltip: str
    variant: str | None  # None preserves transient styling
    disabled: bool


class FetchTab(ScrollableContainer):
    """Content for the Fetch tab."""

    # Limit n/N to the Fetch tab.
    BINDINGS = [
        ("n", "search_next"),
        ("N", "search_prev"),
    ]

    def compose(self) -> ComposeResult:
        # Determine input verb based on terminal
        input_verb = "Enter" if detect_legacy_windows() else "Paste"

        # Simple vertical layout: controls on top, big log on the bottom
        with Vertical(id="fetch_layout"):
            with Grid(id="fetch_grid"):
                yield make_client_select(self.app.current_env, "client_select_fetch")
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
                # Right-hand grid cell even when the server dropdown is hidden
                with Vertical(id="fetch_server_slot"):
                    server_rows, server_value = server_select_state(self.app)
                    yield ServerSelect(server_rows, server_value, "server_select_fetch")
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
        self._log_search_matches = [
            i for i, line in enumerate(log.lines) if term_lower in str(line).lower()
        ]
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

    def _step_search(self, delta: int) -> None:
        self._ensure_log_search_matches_current_term()

        if not self._log_search_matches:
            if self._log_search_term:
                self.app.notify(f"'{self._log_search_term}' not found in log.")
            else:
                self.app.notify("Enter a search term first.")
            return

        self._log_search_index = (
            self._log_search_index + delta
        ) % len(self._log_search_matches)
        self._show_current_log_search_result()

    def action_search_next(self) -> None:
        self._step_search(1)

    def action_search_prev(self) -> None:
        self._step_search(-1)

    def reset_log_search_state(self) -> None:
        """Reset all log search state for this tab."""
        self._log_search_matches = []
        self._log_search_index = -1
        self._log_search_term = ""

    def on_mount(self) -> None:
        for attr in ("mq_down", "is_updating", "progress_visible", "interface_running",
                     "download_folder", "current_env", "active_server", "_offer_active",
                     "update_count", "watched_flash", "provision_running"):
            self.watch(self.app, attr, self._recompute)
        self.watch(self.app, "username", self._refresh_welcome)
        self.watch(self.app, "is_level_2", self._refresh_welcome)

    def _refresh_welcome(self) -> None:
        app = self.app
        if not app.username:
            return  # keep the compose default until identity resolves
        if app.is_level_2 is True:
            greeting = f"[italic]Hail, [bold]{app.username}![/bold][/italic]"
        elif app.is_level_2 is False:
            greeting = f"Hey {app.username}, you're level 1 😞"
        else:
            greeting = f"Hey [bold]{app.username}[/bold]!"
        self.query_one("#welcome_label", Label).update(greeting)

    def _watched_button_state(self) -> WatchedButtonState:
        """Derive button state."""
        app = self.app
        if app.mq_down is None:
            return WatchedButtonState(
                "Checking MQ status...📞",
                "Please wait while we check MQ status.",
                variant=None,
                disabled=True,
            )
        if app.mq_down:
            return WatchedButtonState(
                "MQ Down: Patch Day 💔",
                "Very Vanilla MQ is down for patch day, check redguides.com for current status.",
                variant="default",
                disabled=True,
            )
        if app.provision_running:
            # Not a cancel toggle: this button doesn't own the work that's running.
            return WatchedButtonState(
                "Easy Update Button 🍦",
                "Wait for the server setup to finish.",
                variant="default",
                disabled=True,
            )
        if app.is_updating:
            if app._offer_active:
                # The update is past its cancellable phase.
                return WatchedButtonState(
                    "Finishing update... 🏁",
                    "Waiting on the post-update prompts.",
                    variant=None,
                    disabled=True,
                )
            return WatchedButtonState(
                "Stop Update 🛑", "Update in progress. Click to cancel.", variant=None, disabled=False
            )
        base_tooltip = (
            "Update all resources that you've watched, as well as those we've marked 'special' like Very Vanilla MQ and other staff picks. "
            "(Manage watched resources on the website, and opt-in or out of any 'special' resources in settings.local.toml)"
        )
        count = app.update_count
        if count:
            s = "" if count == 1 else "s"
            label = f"Easy Update Button 🍦 ({count})"
            tooltip = f"{count} resource{s} ready to fetch. {base_tooltip}"
            resting_variant = "primary"
        else:  # 0 is current; None is unchecked.
            label = "Easy Update Button 🍦"
            tooltip = base_tooltip
            resting_variant = "default" if count == 0 else "primary"
        # Transient sync feedback overrides the resting variant.
        variant = app.watched_flash or resting_variant
        return WatchedButtonState(label, tooltip, variant, disabled=not bool(app.download_folder))

    def _recompute(self) -> None:
        """Apply current app state to widgets."""
        app = self.app
        busy = app.is_updating or app.provision_running
        interface_running = app.interface_running
        download_folder = app.download_folder

        update_watched_button = self.query_one("#update_watched", Button)
        label, tooltip, variant, disabled = self._watched_button_state()
        update_watched_button.label = label
        update_watched_button.tooltip = tooltip
        if variant is not None:
            update_watched_button.variant = variant
        update_watched_button.disabled = disabled
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

        selects_busy = busy or interface_running
        self.query_one("#client_select_fetch", Select).disabled = selects_busy
        self.query_one("#server_select_fetch", ServerSelect).disabled = selects_busy
        sync_client_select(self, "client_select_fetch")
        sync_server_select(self, "server_select_fetch")

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
        self._step_search(1)

    @on(Button.Pressed, "#log_search_prev")
    def handle_log_search_prev_pressed(self, event: Button.Pressed) -> None:
        self._step_search(-1)

    @on(Button.Pressed, "#copy_log")
    def handle_copy_log_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_copy_log()

    @on(Button.Pressed, "#clear_log")
    def handle_clear_log_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_clear_log()

    @on(Input.Submitted, "#resource_id_input")
    def handle_resource_id_submitted(self, event: Input.Submitted) -> None:
        self.app.handle_update_resource_id()

    @on(Input.Submitted, "#log_search")
    def handle_log_search_submitted(self, event: Input.Submitted) -> None:
        self._step_search(1)

    @on(Input.Changed, "#resource_id_input")
    def handle_resource_id_changed(self, event: Input.Changed) -> None:
        self._recompute()

    @on(Select.Changed, "#client_select_fetch")
    def handle_client_select_fetch_changed(self, event: Select.Changed) -> None:
        self.app.handle_client_selected(event.value)

    @on(Select.Changed, "#server_select_fetch")
    def handle_server_select_fetch_changed(self, event: Select.Changed) -> None:
        self.app.handle_server_selected(event.value)


# display print statements in the log widget
class PrintCapturingLog(Log):
    def on_mount(self) -> None:
        self.begin_capture_print()

    def on_print(self, event: Print) -> None:
        self.write(event.text)
