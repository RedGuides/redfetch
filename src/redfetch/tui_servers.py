"""The Servers tab and its add / rename / delete dialogs."""
# standard
import os
from pathlib import Path

# third-party
import httpx
from textual_fspicker import FileOpen, SelectDirectory
from rich.console import detect_legacy_windows
from rich.markup import escape
from rich.text import Text

# textual framework
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Input, Label, OptionList, ProgressBar, RadioButton, RadioSet, Select,
)
from textual.widgets.option_list import Option

# local
from redfetch import config
from redfetch import laa
from redfetch import patcher
from redfetch import provision
from redfetch import servers
from redfetch import shortcuts
from redfetch import utils
from redfetch.tui_widgets import BARE_SETUP_ID, clean_source_filters


# The Servers tab surfaces the Shortcuts tab's eqhost.txt entry, rather than its own.
EQHOST_SHORTCUT = shortcuts.find_openable("eqhost")


class ProvisionProgress(Message):
    """A step of a running provision. Posted from the copy thread and the worker alike."""

    def __init__(self, label: str, fraction: float | None) -> None:
        super().__init__()
        self.label = label
        self.fraction = fraction


class ServersTab(ScrollableContainer):
    """Manage a multi-server env's servers."""

    def compose(self) -> ComposeResult:
        input_verb = "Enter" if detect_legacy_windows() else "Paste"
        with Vertical(id="servers_layout"):
            with Horizontal(id="dx9_notice"):
                yield Label(
                    "DirectX 9 wasn't detected on your computer, which EverQuest needs. "
                    f"[@click=app.link('{utils.DX9_INSTALLER_URL}')]Get it from Microsoft[/]",
                    id="dx9_notice_text",
                )
                yield Button(
                    "Dismiss",
                    id="dx9_dismiss",
                    variant="default",
                    tooltip="Hide this warning for good.",
                )
            yield OptionList(id="server_list")
            with Horizontal(id="server_actions"):
                yield Button(
                    "Add",
                    id="server_add",
                    variant="default",
                    tooltip="Set up a known emu server, or add your own.",
                )
                label = servers.active_server_context(self.app.current_env).label
                yield Button(
                    "Get patcher",
                    id="server_patcher",
                    variant="default",
                    tooltip=Content(f"Download the {label} patcher into its EverQuest folder."),
                )
                yield Button(
                    EQHOST_SHORTCUT.label,
                    id="server_eqhost",
                    variant="default",
                    tooltip=EQHOST_SHORTCUT.tooltip,
                )
                yield Button(
                    "Allow 4GB Memory",
                    id="server_laa",
                    variant="default",
                    tooltip="Let eqgame.exe use up to 4GB of memory.",
                )
                yield Button(
                    "Rename",
                    id="server_rename",
                    variant="default",
                    tooltip="Rename a custom server. Known server names come from redfetch.",
                )
                yield Button(
                    "Delete",
                    id="server_delete",
                    variant="error",
                    tooltip="Remove a custom server, or reset a known one back to available.",
                )
            # Only visible during a provision, which is the one action long enough to watch.
            with Horizontal(id="server_provision_row", classes="hidden"):
                # markup=False: the labels quote server names and paths.
                yield Label("", id="provision_status", markup=False)
                yield ProgressBar(total=1.0, show_eta=False, id="provision_progress")
                yield Button("Cancel", id="provision_cancel", variant="error")
            # The same setting as the Settings tab's copy: the active server's folder.
            with Horizontal(id="server_eq_path_row"):
                yield Button(
                    "EverQuest Folder",
                    id="server_select_eq_path",
                    variant="default",
                    tooltip="The active server's EverQuest directory, the one with eqgame.exe.",
                )
                yield Input(
                    value=config.settings.from_env(self.app.current_env).EQPATH or "",
                    placeholder=f"{input_verb} the active server's EverQuest directory",
                    id="server_eq_path_input",
                    tooltip="The active server's EverQuest directory, the one with eqgame.exe.",
                )

    def on_mount(self) -> None:
        for attr in ("is_updating", "interface_running", "current_env", "active_server",
                     "eq_path", "provision_running"):
            self.watch(self.app, attr, self._recompute)

    def on_show(self) -> None:
        self._recompute()  # Pick up external config edits.

    def _highlighted_slug(self) -> str | None:
        option_list = self.query_one("#server_list", OptionList)
        if option_list.highlighted is None:
            return None
        return option_list.get_option_at_index(option_list.highlighted).id

    def _recompute(self) -> None:
        app = self.app
        # Never the whole tab for a provision: that would grey out its own Cancel button.
        self.disabled = app.is_updating or app.interface_running
        # Machine-level, not per-server; the tab itself is the emu gate.
        self.query_one("#dx9_notice").display = utils.dx9_notice_wanted()
        provisioning = app.provision_running
        self.query_one("#server_provision_row").set_class(not provisioning, "hidden")
        self.query_one("#provision_cancel", Button).disabled = not app.provision_cancellable
        if not servers.is_multi_server(app.current_env):
            return
        self.query_one("#server_eq_path_input", Input).value = app.eq_path or ""
        self._rebuild_list()
        self._refresh_buttons()

    def show_provision_progress(self, label: str, fraction: float | None) -> None:
        """Report a step of the running provision."""
        self.query_one("#provision_status", Label).update(label)
        bar = self.query_one("#provision_progress", ProgressBar)
        if fraction is None:
            bar.update(total=None)  # indeterminate! No way to know size
        else:
            bar.update(total=1.0, progress=fraction)
        self.query_one("#provision_cancel", Button).disabled = not self.app.provision_cancellable

    def _rebuild_list(self) -> None:
        app = self.app
        env = app.current_env
        option_list = self.query_one("#server_list", OptionList)
        previous = self._highlighted_slug()
        listed = servers.list_servers(env)
        active = servers.get_active_server(env)
        option_list.clear_options()
        # escape() + Text.from_markup share rich's limited [a-z#/@] tag set.
        # Content.from_markup would eat all [...], breaking names like "PEQ [TAKP]".
        bare_label = config.BARE_SERVER_LABEL
        if active is None:
            bare = f"[green]●[/green] [b]{bare_label}[/b]  [dim]{escape(app.eq_path or '')}[/dim]"
        else:
            bare = f"○ {bare_label}  [dim]{escape(servers.generic_eqpath(env))}[/dim]"
        option_list.add_option(Option(Text.from_markup(bare), id=BARE_SETUP_ID))
        slugs = [BARE_SETUP_ID, *sorted(listed)]
        for slug in slugs[1:]:
            entry = listed[slug]
            label = escape(entry.get("label") or slug)  # Tolerate hand-edited entries.
            eqpath = escape(str(entry.get("eqpath") or ""))
            if slug == active:
                # The active snapshot lags live settings until switch-away.
                eqpath = escape(str(app.eq_path or ""))
                prompt = f"[green]●[/green] [b]{label}[/b]  [dim]{eqpath}[/dim]"
            elif servers.is_server_configured(slug, env):
                prompt = f"○ {label}  [dim]{eqpath}[/dim]"
            elif servers.is_known_server(slug, env):
                prompt = f"[dim]  {label} — available[/dim]"
            else:
                prompt = f"○ {label}  [dim]— not set up[/dim]"
            option_list.add_option(Option(Text.from_markup(prompt), id=slug))
        if previous in slugs:
            option_list.highlighted = slugs.index(previous)
        elif slugs:
            option_list.highlighted = 0

    def _refresh_buttons(self) -> None:
        env = self.app.current_env
        slug = self._highlighted_slug()
        active = servers.get_active_server(env)
        bare = slug == BARE_SETUP_ID
        known = bool(slug) and not bare and servers.is_known_server(slug, env)
        configured = bool(slug) and not bare and servers.is_server_configured(slug, env)
        selected_active = slug == active or (bare and active is None)
        # A provision owns the tab, cancel is the only control left alive
        provisioning = self.app.provision_running
        for widget_id in ("#server_list", "#server_add", "#server_select_eq_path",
                          "#server_eq_path_input"):
            self.query_one(widget_id).disabled = provisioning
        # The bare setup has no entry to rename or delete.
        self.query_one("#server_rename", Button).disabled = (
            provisioning or not slug or known or bare
        )
        # Unconfigured known servers have nothing to delete.
        self.query_one("#server_delete", Button).disabled = (
            provisioning or not slug or bare or (known and not configured)
        )
        self._refresh_patcher_button(env, slug, selected_active)
        self._refresh_eqhost_button(selected_active)
        self._refresh_laa_button(env, selected_active)

    def _refresh_patcher_button(self, env: str, slug: str | None, selected_active: bool) -> None:
        """Enable the patcher button only when the active server has a patcher and it isn't already installed or downloading."""
        button = self.query_one("#server_patcher", Button)
        context = servers.active_server_context(env)
        if selected_active and patcher.has_patcher(context):
            # Content(), not escape()
            label = context.label
            downloading = self.app.patcher_install_running
            enabling = self.app.laa_enable_running
            installed = patcher.is_installed(context)
            button.disabled = installed or downloading or enabling or self.app.provision_running
            if enabling:
                button.tooltip = "Wait for the 4GB memory change to finish."
            elif downloading:
                button.tooltip = Content(f"Downloading the {label} patcher...")
            elif installed:
                button.tooltip = Content(f"The {label} patcher is already in its EverQuest folder.")
            else:
                button.tooltip = Content(f"Download the {label} patcher into its EverQuest folder.")
            return
        button.disabled = True
        entry = (servers.list_servers(env).get(slug) or {}) if slug else {}
        if not entry.get("patcher_url"):
            button.tooltip = "This server has no patcher, according to my settings.local.toml"
        elif selected_active:
            # Reachable only by hand-editing: a link with no file name to install or run.
            button.tooltip = "This server's patcher entry needs a patcher_exe file name in settings.local.toml."
        else:
            button.tooltip = "Switch to this server first, then download its patcher."

    def _refresh_eqhost_button(self, selected_active: bool) -> None:
        """Open the active server's eqhost.txt, naming the login server it holds."""
        button = self.query_one("#server_eqhost", Button)
        if not selected_active:
            # The shortcut always resolves the active server's folder, never the highlighted one.
            button.disabled = True
            button.tooltip = "Switch to this server first, then open its eqhost.txt."
            return
        if not shortcuts.openable_available(EQHOST_SHORTCUT):
            button.disabled = True
            button.tooltip = "No eqhost.txt in this server's EverQuest folder."
            return
        button.disabled = self.app.provision_running
        # Content(), not escape(): the host is read from a file the user can hand-edit.
        button.tooltip = Content(shortcuts.openable_tooltip(EQHOST_SHORTCUT))

    def _refresh_laa_button(self, env: str, selected_active: bool) -> None:
        """Enable the 4GB button only when eqgame lacks the flag."""
        button = self.query_one("#server_laa", Button)
        if selected_active:
            # No per-server key: the folder alone decides.
            context = servers.active_server_context(env)
            info = laa.status(context.eqpath)
            enabling = self.app.laa_enable_running
            downloading = self.app.patcher_install_running
            button.disabled = (
                enabling or downloading or self.app.provision_running
                or info.state is not laa.LaaState.OFF
            )
            if enabling:
                button.tooltip = "Setting the 4GB flag on eqgame.exe..."
            elif downloading:
                # The patcher zip might carry an exe that already has the flag.
                button.tooltip = "Wait for the patcher download to finish."
            elif info.state is laa.LaaState.HIDDEN:
                button.tooltip = "Pick this server's EverQuest folder first, on the Settings tab."
            elif info.state is laa.LaaState.BLOCKED:
                button.tooltip = Content(info.problem)
            elif info.state is laa.LaaState.ON:
                button.tooltip = "eqgame.exe can already use 4GB of memory."
            else:
                button.tooltip = Content(
                    f"Let {context.label}'s eqgame.exe use up to 4GB of memory. "
                    f"The original is kept as {laa.BACKUP_NAME}."
                )
            return
        button.disabled = True
        button.tooltip = "Switch to this server first, then allow its 4GB of memory."

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._refresh_buttons()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self._switch(event.option_id)

    #
    # Actions
    #

    @on(Button.Pressed, "#server_select_eq_path")
    def handle_select_eq_path_pressed(self, event: Button.Pressed) -> None:
        self.app.select_directory("server_eq_path_input")

    @on(Button.Pressed, "#dx9_dismiss")
    def handle_dx9_dismiss_pressed(self, event: Button.Pressed) -> None:
        utils.dismiss_dx9_notice()
        self.query_one("#dx9_notice").display = False

    @on(Input.Submitted, "#server_eq_path_input")
    def handle_eq_path_submitted(self, event: Input.Submitted) -> None:
        self.app.handle_input_update(event.input.id, event.input.value.strip())

    def _busy(self) -> bool:
        """Re-check the busy gate"""
        app = self.app
        if app.is_updating or app.interface_running or app.provision_running:
            app.notify("redfetch is busy; try again when it finishes.", severity="warning")
            return True
        return False

    def _switch(self, slug: str) -> None:
        app = self.app
        env = app.current_env
        active = servers.get_active_server(env)
        if slug == BARE_SETUP_ID:
            if active is not None:
                app.switch_to_bare_setup(env)
            return
        if slug == active:
            return
        if not servers.is_server_configured(slug, env):
            # CLI parity: prompt for the folder
            self._configure_server(slug, switch_after=True)
            return
        app.switch_active_server(slug)

    def _configure_server(self, slug: str, *, switch_after: bool) -> None:
        """Configure a known or custom server."""
        env = self.app.current_env
        label = servers.server_label(slug, env)

        def done(result: dict | None) -> None:
            if not result or self._busy():
                return
            if result["mode"] == AddServerScreen.PROVISION:
                self.app.provision_server(result, switch_after=switch_after)
                return
            try:
                servers.add_server(slug, env=env, eqpath=result["eqpath"])
            except ValueError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.notify(f"{label} set up.", markup=False)
            self.app.refresh_after_server_change()
            if switch_after:
                # add_server never activates, switching is always explicit.
                self.app.switch_active_server(slug)

        self.app.push_screen(AddServerScreen([], locked=(slug, label)), done)

    @on(Button.Pressed, "#server_patcher")
    def handle_patcher_pressed(self, event: Button.Pressed) -> None:
        if self._busy():
            return
        if self.app.install_active_patcher():
            # Greys out this button and Set Login Server until the worker's recompute
            self._refresh_buttons()

    @on(Button.Pressed, "#server_eqhost")
    def handle_eqhost_pressed(self, event: Button.Pressed) -> None:
        self.app.open_target(EQHOST_SHORTCUT)

    @on(Button.Pressed, "#server_laa")
    def handle_laa_pressed(self, event: Button.Pressed) -> None:
        if self._busy():
            return
        self.app.enable_active_laa()
        # Unconditional: a declined stale click heals the button to disabled-with-why.
        self._refresh_buttons()

    @on(Button.Pressed, "#server_add")
    def handle_add_pressed(self, event: Button.Pressed) -> None:
        env = self.app.current_env
        available: list[tuple[str | Text, str]] = [
            # Text(), not escape() or Content()
            (Text(entry.get("label") or slug), slug)
            for slug, entry in sorted(servers.list_servers(env).items())
            if servers.is_known_server(slug, env) and not servers.is_server_configured(slug, env)
        ]

        def done(result: dict | None) -> None:
            if not result or self._busy():
                return
            if result["mode"] == AddServerScreen.PROVISION:
                self.app.provision_server(result)
                return
            try:
                servers.add_server(
                    result["slug"], env=env, eqpath=result["eqpath"], label=result["label"],
                    patcher_url=result.get("patcher_url"),
                    patcher_exe=result.get("patcher_exe"),
                )
            except ValueError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.notify(f"Server '{result['slug']}' added.")
            self.app.refresh_after_server_change()

        self.app.push_screen(AddServerScreen(available), done)

    @on(Button.Pressed, "#provision_cancel")
    def handle_provision_cancel_pressed(self, event: Button.Pressed) -> None:
        self.app.cancel_provision()

    @on(Button.Pressed, "#server_rename")
    def handle_rename_pressed(self, event: Button.Pressed) -> None:
        env = self.app.current_env
        slug = self._highlighted_slug()
        if not slug:
            return

        def done(new_slug: str | None) -> None:
            if not new_slug or new_slug == slug or self._busy():
                return
            try:
                servers.rename_server(slug, new_slug, env=env)
            except ValueError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.notify(f"Server '{slug}' renamed to '{new_slug}'.")
            self.app.refresh_after_server_change()

        self.app.push_screen(RenameServerScreen(slug), done)

    @on(Button.Pressed, "#server_delete")
    def handle_delete_pressed(self, event: Button.Pressed) -> None:
        env = self.app.current_env
        slug = self._highlighted_slug()
        if not slug:
            return
        label = servers.server_label(slug, env)
        known = servers.is_known_server(slug, env)
        is_active = servers.get_active_server(env) == slug
        def done(confirmed: bool | None) -> None:
            if not confirmed or self._busy():
                return
            try:
                servers.delete_server(slug, env=env)
            except ValueError as exc:
                self.app.notify(str(exc), severity="error")
                return
            message = (
                f"{label} reset; it's back in the available list."
                if known else f"{label} removed."
            )
            if is_active:
                message += f" Now on {config.BARE_SERVER_LABEL}."
            self.app.notify(message, markup=False)
            self.app.refresh_after_server_change()

        self.app.push_screen(
            ConfirmDeleteServerScreen(
                label, known=known, is_active=is_active, lands_on=config.BARE_SERVER_LABEL,
            ),
            done,
        )


def source_note() -> Content:
    """The sourcing note."""
    return Content(f"{provision.NO_SOURCE_MESSAGE} {provision.SOURCE_NOTE_DETAIL}")


class AddServerScreen(ModalScreen[dict | None]):
    """Collect a server: an existing EverQuest folder, or a new one from a clean RoF2 copy."""

    CUSTOM = "__custom__"
    BROWSE = "browse"
    PROVISION = "provision"
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, available: list[tuple[str | Text, str]], *,
                 locked: tuple[str, str] | None = None) -> None:
        super().__init__()
        self._available = available
        # (slug, label) when the caller already picked the server for us.
        self._locked = locked
        self._destination_touched = False

    def compose(self) -> ComposeResult:
        if self._locked:
            slug, label = self._locked
            options: list[tuple[str | Text, str]] = [(Text(label), slug)]
        else:
            options = [*self._available, ("Custom server…", self.CUSTOM)]
        provisioning = self._default_mode() == self.PROVISION
        with Vertical(id="server_dialog"):
            yield Label("Add an emu server", id="server_dialog_title")
            # can_focus=False, or the scroll container steals the dialog's opening focus.
            with VerticalScroll(id="server_dialog_body", can_focus=False):
                yield Select[str](options, id="add_known", allow_blank=False,
                                  value=options[0][1], disabled=bool(self._locked))
                yield Input(placeholder="Name (e.g. The Grind)", id="add_label")
                yield Input(placeholder="Short name: a-z 0-9 - _ (e.g. thegrind)", id="add_slug")
                with RadioSet(id="add_mode"):
                    yield RadioButton("Use an existing EverQuest folder",
                                      value=not provisioning, id="add_mode_browse")
                    yield RadioButton("Create a new folder from a clean RoF2 copy",
                                      value=provisioning, id="add_mode_provision")
                yield Label(provision.STEPS_NOTE, id="add_provision_steps")
                # Source first, then destination: the order the copy runs in.
                yield Label("Source — your clean RoF2 copy", id="add_source_label")
                with Horizontal(id="add_source_row"):
                    yield Input(value=provision.clean_source(),
                                placeholder="A zip, an iso, a DVD drive, or a folder",
                                id="add_source")
                    yield Button("File…", id="add_source_file", variant="default")
                    yield Button("Folder…", id="add_source_folder", variant="default")
                yield Label(source_note(), id="add_source_note")
                # _sync_mode names it: the same box is a destination or an existing folder.
                yield Label("", id="add_folder_label")
                with Horizontal(id="add_folder_row"):
                    yield Input(placeholder="EverQuest folder for this server", id="add_folder")
                    yield Button("Browse", id="add_browse", variant="default")
                yield Input(placeholder="Patcher download link (optional)", id="add_patcher_url")
                yield Input(placeholder="Patcher file name (optional, e.g. ThePatcher.exe)", id="add_patcher_exe")
            # markup=False: these errors quote what the user typed, brackets and all.
            yield Label("", id="server_dialog_error", markup=False)
            with Horizontal(id="server_dialog_buttons"):
                yield Button("Add", id="add_confirm", variant="primary")
                yield Button("Cancel", id="add_cancel", variant="default")

    def on_mount(self) -> None:
        self._sync_mode()
        self._sync_destination()

    def _default_mode(self) -> str:
        """The clean copy leads once there's one to provision from."""
        source = provision.clean_source()
        return self.PROVISION if source and os.path.exists(source) else self.BROWSE

    def _is_custom(self) -> bool:
        return self.query_one("#add_known", Select).value == self.CUSTOM

    def _is_provision(self) -> bool:
        return self.query_one("#add_mode_provision", RadioButton).value

    def _current_slug(self) -> str:
        if self._locked:
            return self._locked[0]
        if self._is_custom():
            return self.query_one("#add_slug", Input).value.strip()
        return str(self.query_one("#add_known", Select).value)

    def _sync_mode(self) -> None:
        custom = self._is_custom()
        provisioning = self._is_provision()
        for widget_id in ("#add_label", "#add_slug", "#add_patcher_url", "#add_patcher_exe"):
            self.query_one(widget_id, Input).display = custom
        for widget_id in ("#add_source_label", "#add_source_row"):
            self.query_one(widget_id).display = provisioning
        needs_source = provisioning and not provision.clean_source()
        self.query_one("#add_source_note", Label).display = needs_source
        # Named up front, so the whole chain is the user's own choice.
        self.query_one("#add_provision_steps", Label).display = provisioning
        # A folder that doesn't exist yet is nothing to browse to.
        self.query_one("#add_browse", Button).display = not provisioning
        self.query_one("#add_folder_label", Label).update(
            "Destination — the new EverQuest folder to create" if provisioning
            else "This server's EverQuest folder"
        )
        self.query_one("#add_folder", Input).placeholder = (
            "Where to create the new EverQuest folder" if provisioning
            else "EverQuest folder for this server"
        )

    def _sync_destination(self) -> None:
        """Keep the destination on the chosen server, until the user picks their own."""
        if self._destination_touched or not self._is_provision():
            return
        try:
            slug = servers.validate_server_slug(self._current_slug())
        except ValueError:
            return 
        with self.prevent(Input.Changed):
            self.query_one("#add_folder", Input).value = provision.default_destination(slug)

    @on(Select.Changed, "#add_known")
    def handle_known_changed(self, event: Select.Changed) -> None:
        self._sync_mode()
        self._sync_destination()

    @on(RadioSet.Changed, "#add_mode")
    def handle_mode_changed(self, event: RadioSet.Changed) -> None:
        if self._is_provision():
            self._destination_touched = False
        self._sync_mode()
        self._sync_destination()

    @on(Input.Changed, "#add_slug")
    def handle_slug_changed(self, event: Input.Changed) -> None:
        self._sync_destination()

    @on(Input.Changed, "#add_folder")
    def handle_folder_changed(self, event: Input.Changed) -> None:
        self._destination_touched = True

    @on(Button.Pressed, "#add_browse")
    def handle_browse(self, event: Button.Pressed) -> None:
        current = self.query_one("#add_folder", Input).value.strip()
        start = Path(current) if current and Path(current).is_dir() else Path.home()

        def picked(path: Path | None) -> None:
            if path:
                self.query_one("#add_folder", Input).value = str(path)

        self.app.push_screen(SelectDirectory(location=start), callback=picked)

    @on(Button.Pressed, "#add_source_file")
    def handle_source_file(self, event: Button.Pressed) -> None:
        self._pick_source(
            FileOpen(location=self._source_start(), filters=clean_source_filters())
        )

    @on(Button.Pressed, "#add_source_folder")
    def handle_source_folder(self, event: Button.Pressed) -> None:
        self._pick_source(SelectDirectory(location=self._source_start()))

    def _source_start(self) -> Path:
        return utils.nearest_dir(self.query_one("#add_source", Input).value.strip())

    def _pick_source(self, picker: ModalScreen) -> None:
        def picked(path: Path | None) -> None:
            if not path:
                return
            self.query_one("#add_source", Input).value = str(path)
            self._remember_source(str(path))

        self.app.push_screen(picker, callback=picked)

    def _remember_source(self, path: str) -> bool:
        """Save the first source they name, so the next server needs no typing."""
        if provision.clean_source():
            return True
        try:
            provision.set_clean_source(path)
        except provision.ProvisionError as exc:
            self.query_one("#server_dialog_error", Label).update(str(exc))
            return False
        self._sync_mode()
        return True

    def _confirm(self) -> None:
        error = self.query_one("#server_dialog_error", Label)
        folder = self.query_one("#add_folder", Input).value.strip()
        patcher_url = ""
        patcher_exe = ""
        if self._locked:
            # The entry is already there; only its folder is missing.
            slug = self._locked[0]
            label = None
        elif self._is_custom():
            slug = self.query_one("#add_slug", Input).value.strip()
            label = self.query_one("#add_label", Input).value.strip() or None
            try:
                servers.validate_server_slug(slug, must_be_new=True)
            except ValueError as exc:
                error.update(str(exc))
                return
            patcher_url = self.query_one("#add_patcher_url", Input).value.strip()
            if patcher_url:
                try:
                    parsed = httpx.URL(patcher_url)
                except httpx.InvalidURL:
                    parsed = None
                if parsed is None or parsed.scheme not in ("https", "http") or not parsed.host:
                    error.update("The patcher link must be a full web link, like https://...")
                    return
            patcher_exe = self.query_one("#add_patcher_exe", Input).value.strip()
            if patcher_exe:
                try:
                    patcher_exe = patcher.validate_patcher_exe(patcher_exe)
                except patcher.PatcherError as exc:
                    error.update(str(exc))
                    return
            if bool(patcher_url) != bool(patcher_exe):
                # Only useful whole: the link fetches it, the name installs and runs it.
                error.update(
                    "Add the patcher's file name too, like ThePatcher.exe."
                    if patcher_url
                    else "Add the patcher's download link too."
                )
                return
        else:
            slug = str(self.query_one("#add_known", Select).value)
            label = None

        if self._is_provision():
            source = self.query_one("#add_source", Input).value.strip()
            if not folder:
                error.update("Choose where to create the new EverQuest folder.")
                return
            try:
                provision.classify_source(source)
                provision.validate_destination(Path(folder), Path(source))
            except provision.ProvisionError as exc:
                error.update(str(exc))
                return
            if not self._remember_source(source):
                return
            self.dismiss(
                {"mode": self.PROVISION, "slug": slug, "label": label,
                 "source": source, "destination": folder,
                 "patcher_url": patcher_url, "patcher_exe": patcher_exe}
            )
            return

        if not folder:
            error.update("Choose the EverQuest folder for this server.")
            return
        if not utils.validate_file_in_path(folder, "eqgame.exe"):
            error.update("No eqgame.exe in that folder, so it isn't an EverQuest folder.")
            return
        self.dismiss(
            {"mode": self.BROWSE, "slug": slug, "label": label, "eqpath": folder,
             "patcher_url": patcher_url, "patcher_exe": patcher_exe}
        )

    @on(Button.Pressed, "#add_confirm")
    def handle_confirm(self, event: Button.Pressed) -> None:
        self._confirm()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()

    @on(Button.Pressed, "#add_cancel")
    def handle_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class RenameServerScreen(ModalScreen[str | None]):
    """Rename a custom server."""

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, slug: str) -> None:
        super().__init__()
        self._slug = slug

    def compose(self) -> ComposeResult:
        with Vertical(id="server_dialog"):
            yield Label(f"Rename '{self._slug}'", id="server_dialog_title", markup=False)
            yield Input(placeholder="New short name: a-z 0-9 - _", id="rename_slug")
            # markup=False: validate_server_slug quotes the raw slug the user typed.
            yield Label("", id="server_dialog_error", markup=False)
            with Horizontal(id="server_dialog_buttons"):
                yield Button("Rename", id="rename_confirm", variant="primary")
                yield Button("Cancel", id="rename_cancel", variant="default")

    def _confirm(self) -> None:
        new_slug = self.query_one("#rename_slug", Input).value.strip()
        try:
            servers.validate_server_slug(new_slug, must_be_new=True)
        except ValueError as exc:
            self.query_one("#server_dialog_error", Label).update(str(exc))
            return
        self.dismiss(new_slug)

    @on(Button.Pressed, "#rename_confirm")
    def handle_confirm(self, event: Button.Pressed) -> None:
        self._confirm()

    @on(Button.Pressed, "#rename_cancel")
    def handle_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()


class ConfirmDeleteServerScreen(ModalScreen[bool]):
    """Confirm a server reset or deletion."""

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def __init__(self, label: str, *, known: bool, is_active: bool,
                 lands_on: str) -> None:
        super().__init__()
        self._label = label
        self._known = known
        self._is_active = is_active
        self._lands_on = lands_on

    def compose(self) -> ComposeResult:
        if self._known:
            message = (
                f"Reset {self._label}? Your folder and map choices for it are removed, "
                "and it goes back to the available list."
            )
        else:
            message = f"Delete {self._label}? Its settings are removed from settings.local.toml."
        if self._is_active:
            message += f"\n\nThis is your active server, so you'll switch to {self._lands_on}."
        with Vertical(id="server_dialog"):
            yield Label(message, id="server_dialog_title", markup=False)
            with Horizontal(id="server_dialog_buttons"):
                yield Button("Reset" if self._known else "Delete", id="delete_confirm", variant="error")
                yield Button("Cancel", id="delete_cancel", variant="default")

    @on(Button.Pressed, "#delete_confirm")
    def handle_confirm(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#delete_cancel")
    def handle_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(False)
