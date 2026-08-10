"""The Settings tab: paths, launch options, and the staff-pick bundle."""
# standard
import sys

# third-party
from rich.console import detect_legacy_windows

# textual framework
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, ItemGrid, ScrollableContainer
from textual.content import Content
from textual.widgets import Button, Checkbox, Input, Label, RadioSet, Select, Switch

# local
from redfetch import config
from redfetch import desktop_shortcut
from redfetch import navmesh
from redfetch import provision
from redfetch import servers
from redfetch import utils
from redfetch.tui_widgets import (
    BARE_SETUP_ID, ServerSelect, TRISTATE_OPTIONS, make_client_select, make_tristate,
    server_select_state, set_tristate, sync_client_select, sync_server_select,
)


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


class SettingsTab(ScrollableContainer):
    """Content for the Settings tab."""

    def compose(self) -> ComposeResult:
        input_verb = "Enter" if detect_legacy_windows() else "Paste"
        current_env = self.app.current_env

        with Horizontal(id="dropdowns_grid"):
            yield make_client_select(self.app.current_env, "client_select")
            server_rows, server_value = server_select_state(self.app)
            yield ServerSelect(server_rows, server_value, "server_select")
        with ItemGrid(id="server_settings_grid", classes="bordertitles"):
            yield Button(
                "EverQuest Folder",
                id="select_eq_path",
                variant="default",
                tooltip=(
                    "The EverQuest directory, the one with eqgame.exe."
                ),
            )
            yield Input(
                value=config.settings.from_env(current_env).EQPATH or "",
                placeholder=f"{input_verb} your EverQuest directory",
                id="eq_path_input",
                tooltip=(
                    "The EverQuest directory, the one with eqgame.exe."
                ),
                valid_empty=True,
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
                    value="VVMQ not available for this client",
                    id="vvmq_path_input",
                    disabled=True,
                )
            clean_source_tooltip = (
                "Your untouched copy of EverQuest RoF2. Can be a zip, an ISO, a folder, or DVD drive. "
            )
            yield Button(
                "Clean RoF2 Copy",
                id="select_clean_source",
                variant="default",
                tooltip=clean_source_tooltip,
            )
            yield Input(
                value=provision.clean_source(),
                placeholder=f"{input_verb} a clean RoF2 archive or folder",
                id="clean_source_input",
                tooltip=clean_source_tooltip,
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
                    "for your selected client."
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
            yield Label("Staff Picks:", classes="left_middle")
            yield Switch(
                id="staff_picks",
                value=staff_picks_enabled(current_env),
                tooltip="A collection of scripts for this client that RedGuides staff recommends.",
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
        for attr in ("current_env", "active_server", "download_folder", "eq_path",
                     "is_updating", "interface_running", "provision_running"):
            self.watch(self.app, attr, self._recompute)

    def on_show(self) -> None:
        # Only the shortcut switch needs an fs re-probe; _recompute would clobber path-input text
        self._refresh_desktop_shortcut()

    def _recompute(self) -> None:
        """Derive every Settings widget from app state + per-env config."""
        app = self.app
        busy = app.is_updating or app.interface_running or app.provision_running

        # Disable entire tab while busy
        self.disabled = busy

        # Path inputs and selection buttons depend on download folder
        has_download = bool(app.download_folder)
        self.query_one("#vvmq_path_input", Input).disabled = not has_download
        self.query_one("#select_vvmq_path", Button).disabled = not has_download

        # Only emu clients provision. Both cells, or the grid's later rows shift.
        provisions = servers.is_multi_server(app.current_env)
        self.query_one("#select_clean_source", Button).display = provisions
        clean_source_input = self.query_one("#clean_source_input", Input)
        clean_source_input.display = provisions
        clean_source_input.value = provision.clean_source()

        sync_client_select(self, "client_select")
        sync_server_select(self, "server_select")

        # Read the label from the dropdown, not from config, so they always match.
        if servers.is_multi_server(app.current_env):
            _, selected = server_select_state(app)
            group_label = (
                config.BARE_SERVER_LABEL if selected == BARE_SETUP_ID
                else servers.server_label(selected, app.current_env)
            )
        else:
            group_label = config.ENVS[app.current_env]
        self.query_one("#server_settings_grid").border_title = Content(f"Server: {group_label}")

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
            vvmq_input_widget.value = "VVMQ not found for this client."
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

    @on(Button.Pressed, "#select_clean_source")
    def handle_select_clean_source_pressed(self, event: Button.Pressed) -> None:
        self.app.select_clean_source("clean_source_input")

    @on(Button.Pressed, "#reset_downloads")
    def handle_reset_downloads_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_reset_downloads()

    @on(Button.Pressed, "#uninstall")
    def handle_uninstall_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_uninstall()

    @on(Input.Submitted, "#dl_path_input, #eq_path_input, #vvmq_path_input, #clean_source_input")
    def handle_path_submitted(self, event: Input.Submitted) -> None:
        self.app.handle_input_update(event.input.id, event.input.value.strip())

    @on(Switch.Changed, "#myseq")
    def handle_myseq_changed(self, event: Switch.Changed) -> None:
        self.app.handle_toggle_myseq(event.value)

    @on(Switch.Changed, "#staff_picks")
    def handle_staff_picks_changed(self, event: Switch.Changed) -> None:
        self.app.handle_toggle_staff_picks(event.value)

    @on(Switch.Changed, "#navmesh")
    def handle_navmesh_changed(self, event: Switch.Changed) -> None:
        self.app.handle_toggle_navmesh(event.value)

    @on(Switch.Changed, "#auto_update")
    def handle_auto_update_changed(self, event: Switch.Changed) -> None:
        self.app.handle_toggle_auto_update(event.value)

    @on(Switch.Changed, "#desktop_shortcut")
    def handle_desktop_shortcut_changed(self, event: Switch.Changed) -> None:
        self.app.handle_toggle_desktop_shortcut(event.value)

    @on(RadioSet.Changed, "#auto_run_vvmq")
    def handle_auto_run_vvmq_changed(self, event: RadioSet.Changed) -> None:
        self.app.handle_toggle_auto_run_vvmq(TRISTATE_OPTIONS[event.index][1])

    @on(Select.Changed, "#eq_maps")
    def handle_eq_maps_changed(self, event: Select.Changed) -> None:
        if event.value != self.app.get_current_eq_maps_value():
            self.app.update_eq_maps_settings(event.value)

    @on(Select.Changed, "#client_select")
    def handle_client_select_changed(self, event: Select.Changed) -> None:
        self.app.handle_client_selected(event.value)

    @on(Select.Changed, "#server_select")
    def handle_server_select_changed(self, event: Select.Changed) -> None:
        self.app.handle_server_selected(event.value)

    @on(Checkbox.Changed, "#post_update_launch Checkbox")
    def on_launch_toggle_changed(self, event: Checkbox.Changed) -> None:
        target = (event.checkbox.id or "").removeprefix("launch_")
        if target:
            self.app.handle_toggle_post_update_launch(target, event.value)
