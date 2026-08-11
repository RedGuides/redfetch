"""App-driven modal screens."""
# standard
import asyncio

# textual framework
from textual import on
from textual.app import ComposeResult
from textual.containers import Center, Grid
from textual.screen import ModalScreen
from textual.widgets import Button, Label

# local
from redfetch import post_update
from redfetch import processes


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

    @on(Button.Pressed, "#yesmq")
    def handle_yes_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.RESPONSE_RUN)

    @on(Button.Pressed, "#alwaysmq")
    def handle_always_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.RESPONSE_ALWAYS)

    @on(Button.Pressed, "#nevermq")
    def handle_never_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.RESPONSE_NEVER)

    @on(Button.Pressed, "#nomq")
    def handle_no_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.RESPONSE_SKIP)


class SourceTypeScreen(ModalScreen[str | None]):
    """File or a folder picker."""

    FILE = "file"
    FOLDER = "folder"

    BINDINGS = [("escape", "dismiss", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(
                "Is your clean RoF2 copy a file (zip, iso)\n"
                "or a folder (DVD drive, folder)?",
                id="question",
            ),
            Button("File", variant="primary", id="source_file"),
            Button("Folder", variant="primary", id="source_folder"),
            Center(Button("Cancel", variant="default", id="source_cancel")),
            id="dialog",
        )

    @on(Button.Pressed, "#source_file")
    def handle_file_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.FILE)

    @on(Button.Pressed, "#source_folder")
    def handle_folder_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.FOLDER)

    @on(Button.Pressed, "#source_cancel")
    def handle_cancel_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


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

    @on(Button.Pressed, "#yes_uninstall")
    def handle_yes_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.RESPONSE_YES)

    @on(Button.Pressed, "#no_uninstall")
    def handle_no_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.RESPONSE_NO)
