"""Post-update policy: does a staged update need a restart to go live?"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal, Protocol

from redfetch import config
from redfetch import processes
from redfetch import utils
from redfetch.sync_types import SyncOutcome


class Decision(Enum):
    RESTART = auto()
    COLD_START = auto()
    NONE = auto()


ColdStartChoice = Literal["yes", "no", "always", "never"]


class PostUpdateSurface(Protocol):
    """This is what the UIs implement."""
    def notify(self, message: str, *, error: bool = False) -> None: ...
    async def confirm_restart(self) -> bool: ...
    async def ask_cold_start(self) -> ColdStartChoice: ...
    def auto_run_persisted(self, value: bool) -> None: ...  # UI sync only; the write already happened
    async def wait_for_eq_close(self) -> bool: ...


def decide(outcome: SyncOutcome, *, mq_running: bool) -> Decision:
    """Only a VVMQ/loader update gets an offer, anything else needs a manual restart."""
    if not outcome.vvmq_updated:
        return Decision.NONE
    return Decision.RESTART if mq_running else Decision.COLD_START


@dataclass(frozen=True, slots=True)
class PendingOffer:
    decision: Decision
    running: set[str] | None
    mq_folder: str | None


async def prepare(outcome: SyncOutcome) -> PendingOffer:
    """Scan and decide — call while the caller still has the UI gated (env, re-clicks)."""
    if (
        sys.platform != "win32"
        or os.environ.get("CI") == "true"
        # decide()'s NONE clause, duplicated so the common no-offer run skips the scan
        or not outcome.vvmq_updated
    ):
        return PendingOffer(Decision.NONE, None, None)
    running = await asyncio.to_thread(processes.running_executable_paths)
    mq_running = await asyncio.to_thread(utils.macroquest_running, running)
    return PendingOffer(decide(outcome, mq_running=mq_running), running, utils.get_vvmq_path())


async def offer(outcome, surface: PostUpdateSurface) -> None:
    """Scan, decide, and execute the post-update offer for a finished sync."""
    await execute(await prepare(outcome), surface)


async def execute(pending: PendingOffer, surface: PostUpdateSurface) -> None:
    # prepare() is the only producer; platform/CI gating already happened there
    if pending.decision is Decision.NONE:
        return
    mq_folder = pending.mq_folder
    if not mq_folder:
        surface.notify("MacroQuest path not found. Please check your configuration.", error=True)
        return

    if pending.decision is Decision.RESTART:
        # a restart always asks; AUTO_RUN_VVMQ governs cold starts only
        if not await surface.confirm_restart():
            surface.notify("The update will apply next time MacroQuest starts.")
            return
        # never touch the live game: the user closes EQ or the restart is skipped
        if await asyncio.to_thread(processes.get_eqgame_process_pids):
            if not await surface.wait_for_eq_close():
                surface.notify("Restart skipped; the update will apply next time MacroQuest starts.")
                return
        try:
            await asyncio.to_thread(processes.restart_macroquest, mq_folder)
        except Exception as exc:
            surface.notify(
                f"Failed to restart MacroQuest: {exc}. "
                "The update is already applied; start MacroQuest manually if needed.",
                error=True,
            )
            return
        # rescan: the restart closed the folder's processes; the old snapshot would skip them
        running = await asyncio.to_thread(processes.running_executable_paths)
    else:
        if not await _cold_start_consent(surface):
            return
        # rescan: the user may have started MQ while the prompt sat open
        running = await asyncio.to_thread(processes.running_executable_paths)
        if not utils.should_offer_mq_start(running):
            surface.notify("MacroQuest is already running; skipping the start.")
            return
        try:
            await asyncio.to_thread(processes.run_executable, mq_folder, "MacroQuest.exe")
        except Exception as exc:
            surface.notify(f"Failed to start MacroQuest: {exc}", error=True)
            return
    _launch_loadout(surface, running)


def _launch_loadout(surface: PostUpdateSurface, running: set[str] | None) -> None:
    """Launch the opt-in loadout, skipping anything already running."""
    to_run, skipped = utils.resolve_post_update_launch_filtered(config.settings.ENV, running)
    for program in skipped:
        surface.notify(f"{os.path.basename(program)} is already running; not starting another.")
    for command, cwd in to_run:
        try:
            processes.run_command(command, cwd)
        except Exception as exc:
            # a typo'd custom command must not kill the rest of the batch (or the CLI)
            label = os.path.basename(utils._command_program(command)) or "post-update program"
            surface.notify(f"Failed to start {label}: {exc}", error=True)


async def _cold_start_consent(surface: PostUpdateSurface) -> bool:
    auto_run = config.settings.from_env(config.settings.ENV).get("AUTO_RUN_VVMQ", None)
    if auto_run is not None:
        return bool(auto_run)
    choice = await surface.ask_cold_start()
    if choice in ("always", "never"):
        value = choice == "always"
        config.update_setting(["AUTO_RUN_VVMQ"], value)
        surface.notify(f"Updated settings to {'always' if value else 'never'} start MacroQuest after updates.")
        surface.auto_run_persisted(value)
    elif choice == "no":
        surface.notify("Not starting MacroQuest.")
    return choice in ("yes", "always")
