"""The Shortcuts tab: buttons for the runnables and folders we know about."""
# textual framework
from textual import on
from textual.app import ComposeResult
from textual.containers import ItemGrid, ScrollableContainer
from textual.content import Content
from textual.widgets import Button

# local
from redfetch import shortcuts


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
        # active_server matters even when eq_path doesn't change.
        for attr in ("is_updating", "download_folder", "eq_path", "current_env",
                     "active_server", "provision_running"):
            self.watch(self.app, attr, self._recompute)

    def on_show(self) -> None:
        self._recompute()  # external installs/deletes fire no reactive; re-probe fs on show

    def _recompute(self) -> None:
        """Enable each shortcut only when its target resolves on disk."""
        self.disabled = self.app.is_updating or self.app.provision_running
        for runnable in shortcuts.RUNNABLES:
            button = self.query_one(f"#run_{runnable.key}", Button)
            # Use Content, not escape(): Rich only escapes [a-z#/@]-initial tags,
            # but Textual strips all [...], mangling "PEQ [TAKP]".
            button.tooltip = Content(shortcuts.runnable_tooltip(runnable))
            # Entries this client or server has no use for hide instead of erroring.
            button.display = shortcuts.runnable_visible(runnable)
            button.disabled = not shortcuts.runnable_available(runnable)
        for openable in shortcuts.OPENABLES:
            button = self.query_one(f"#open_{openable.key}", Button)
            button.disabled = not shortcuts.openable_available(openable)
            # Content(): an entry that reads its file can put anything in the hover text.
            button.tooltip = Content(shortcuts.openable_tooltip(openable))

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
