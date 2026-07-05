"""Post-update launch/restart policy for completed sync runs."""

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
    """UI callbacks used by the post-update workflow."""
    def notify(self, message: str, *, error: bool = False) -> None: ...
    async def confirm_restart(self) -> bool: ...
    async def ask_cold_start(self) -> ColdStartChoice: ...
    def auto_run_persisted(self, value: bool) -> None: ...  # Refresh UI state; the write already happened.
    async def wait_for_eq_close(self) -> bool: ...


def decide(outcome: SyncOutcome, *, mq_running: bool) -> Decision:
    """Return the restart, cold-start, or no-op decision."""
    if mq_running:
        return Decision.RESTART if outcome.vvmq_updated else Decision.NONE
    return Decision.COLD_START if (outcome.success or outcome.vvmq_updated) else Decision.NONE


@dataclass(frozen=True, slots=True)
class PendingOffer:
    decision: Decision
    running: set[str] | None
    mq_folder: str | None


async def prepare(outcome: SyncOutcome) -> PendingOffer:
    """Scan process state and build the pending post-update offer."""
    if (
        sys.platform != "win32"
        or os.environ.get("CI") == "true"
        # Avoid process scans when the run failed before writing anything useful.
        or not (outcome.success or outcome.vvmq_updated)
    ):
        return PendingOffer(Decision.NONE, None, None)
    running = await asyncio.to_thread(processes.running_executable_paths)
    mq_running = await asyncio.to_thread(utils.macroquest_running, running)
    return PendingOffer(decide(outcome, mq_running=mq_running), running, utils.get_vvmq_path())


async def offer(outcome, surface: PostUpdateSurface) -> None:
    """Scan, decide, and execute the post-update offer for a finished sync."""
    await execute(await prepare(outcome), surface)


async def execute(pending: PendingOffer, surface: PostUpdateSurface) -> None:
    # prepare() already handled platform and CI gating.
    if pending.decision is Decision.NONE:
        return
    mq_folder = pending.mq_folder
    if not mq_folder:
        # Cold starts without a folder have nothing to launch.
        if pending.decision is Decision.RESTART:
            surface.notify("MacroQuest path not found. Please check your configuration.", error=True)
        return

    if pending.decision is Decision.RESTART:
        # Restarts always ask; AUTO_RUN_VVMQ only applies to cold starts.
        if not await surface.confirm_restart():
            surface.notify("The update will apply next time MacroQuest starts.")
            return
        # Do not restart MQ while EverQuest is running.
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
        # Rescan so loadout filtering sees the current process list.
        running = await asyncio.to_thread(processes.running_executable_paths)
    else:
        if not await _cold_start_consent(surface):
            return
        # Rescan in case MQ was started while the prompt was open.
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
            # A bad custom command should not stop the remaining launch commands.
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
        surface.notify(f"Updated settings to {'always' if value else 'never'} start MacroQuest after an update run.")
        surface.auto_run_persisted(value)
    elif choice == "no":
        surface.notify("Not starting MacroQuest.")
    return choice in ("yes", "always")
