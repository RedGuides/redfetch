"""Miscellaneous helpers, mostly path resolution and URL parsing."""

# Standard
import os
import re
import shlex
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

# Local
from redfetch import config

#
# path functions
#

def resolve_special_destination(special_resource: dict | None, download_folder: str) -> str | None:
    """Resolve a special resource destination path without side effects."""
    if not special_resource:
        return None
    custom_path = special_resource.get("custom_path")
    if custom_path:
        return os.path.normpath(os.path.realpath(custom_path))
    default_path = special_resource.get("default_path")
    if default_path:
        return os.path.normpath(os.path.join(download_folder, default_path))
    return None


def _resolve_current_special_path(resource_id: str) -> str | None:
    """Resolve the path for a special resource in the current environment."""
    settings = config.active_settings()
    return resolve_special_destination(
        settings.SPECIAL_RESOURCES.get(resource_id), settings.DOWNLOAD_FOLDER
    )


def is_safe_path(base_directory: str, target_path: str) -> bool:
    """Directory-traversal check; is_relative_to is False (not ValueError) across drives."""
    abs_base = os.path.realpath(base_directory)
    abs_target = os.path.realpath(target_path)
    return Path(abs_target).is_relative_to(abs_base)


def get_current_vvmq_id(settings_env: str | None = None) -> str | None:
    env = (settings_env or config.settings.ENV).upper()
    for resource_id, env_name in config.VANILLA_MAP.items():
        if env_name.upper() == env:
            return str(resource_id)
    return None


def get_vvmq_path() -> str | None:
    vvmq_id = get_current_vvmq_id()
    if not vvmq_id:
        return None
    return _resolve_current_special_path(vvmq_id)


def is_auto_update_enabled() -> bool:
    """Whether silent runs may install updates."""
    try:
        return bool(config.active_settings().get("AUTO_UPDATE", True))
    except Exception:
        return False


def get_current_myseq_id() -> str | None:
    current_env = config.settings.ENV.upper()
    for resource_id, env_name in config.MYSEQ_MAP.items():
        if env_name.upper() == current_env:
            return str(resource_id)
    return None


def get_myseq_path() -> str | None:
    myseq_id = get_current_myseq_id()
    if not myseq_id:
        return None
    return _resolve_current_special_path(myseq_id)


def macroquest_running(running: set[str] | None = None) -> bool:
    """Session detection — the loader never runs as the canonical MacroQuest.exe."""
    from redfetch import processes

    if sys.platform != "win32":
        return False
    vvmq = get_vvmq_path()
    return bool(vvmq) and processes.macroquest_session_running(vvmq, running)


def should_offer_mq_start(running: set[str] | None = None) -> bool:
    """MQ is configured and no session is running — the cold-start surface guard."""
    from redfetch import processes

    if sys.platform != "win32":
        return False
    vvmq = get_vvmq_path()
    return bool(vvmq) and not processes.macroquest_session_running(vvmq, running)


def sweep_stale_update_debris() -> None:
    """Sweep .rfnew/.rfold debris across install dirs at session start."""
    from redfetch import download

    for get_path in (get_vvmq_path, get_myseq_path, get_current_download_folder):
        with suppress(Exception):
            path = get_path()
            if path and os.path.isdir(path):
                download.sweep_stale_swap_files(path)


def get_current_download_folder() -> str:
    return os.path.normpath(config.active_settings().DOWNLOAD_FOLDER)


def get_eq_maps_status() -> str | None:
    """Get the status of EQ maps (Brewall's and Good's)."""
    special_resources = config.active_settings().SPECIAL_RESOURCES
    brewall_opt_in = special_resources.get(config.MAPS_MAP["brewall"], {}).get('opt_in', False)
    good_opt_in = special_resources.get(config.MAPS_MAP["good"], {}).get('opt_in', False)

    if brewall_opt_in and good_opt_in:
        return "all"
    elif brewall_opt_in:
        return "brewall"
    elif good_opt_in:
        return "good"
    else:
        return None


def parse_resource_id(input_string: str) -> str:
    # Check if it's already a number
    if input_string.isdigit():
        return input_string

    # Parse the URL
    parsed_url = urlparse(input_string)

    # Check if it's a redguides.com URL
    if not parsed_url.netloc.endswith('redguides.com'):
        raise ValueError("Invalid URL: Neither a redguides.com URL nor a valid resource id")

    # Check if it's a thread URL
    if 'threads' in parsed_url.path:
        raise ValueError("Invalid URL: This appears to be a discussion thread, not a resource")

    # Extract the resource ID using regex
    match = re.search(r'\.(\d+)(?:/|$)', parsed_url.path)
    if match:
        return str(match.group(1))
    else:
        raise ValueError("Could not find a valid resource ID in the URL")


def validate_file_in_path(path: str | None, filename: str) -> bool:
    """Validate that the given path contains a specific file."""
    if not path:
        return False
    try:
        return os.path.isfile(os.path.join(path, filename))
    except (TypeError, ValueError):
        return False


#
# post-update launch
#

@dataclass(frozen=True, slots=True)
class LaunchCommand:
    """A resolved post-update launch."""
    command: list[str] | str    # argv, or a Windows shell string for custom targets
    cwd: str | None = None

    @property
    def program(self) -> str:
        """The program (first token) of the command."""
        return _command_program(self.command)


@dataclass(frozen=True, slots=True)
class FilteredLaunch:
    """A launch plan split by what's already running."""
    to_run: list[LaunchCommand]
    skipped: list[str]          # absolute paths of programs already running


class Preset(NamedTuple):
    resolve_dir: Callable[[], str | None]
    executable: str


# Presets offered as "Also start post-update" toggles.
POST_UPDATE_PRESETS = {
    "eqbcs": Preset(get_vvmq_path, "EQBCS.exe"),
    "myseq": Preset(get_myseq_path, "MySEQ.exe"),
}

POST_UPDATE_PRESET_LABELS = {
    "eqbcs": "EQBCS",
    "myseq": "MySEQ",
    "custom": "Custom",
}


def post_update_launch_choices() -> list[tuple[str, str]]:
    """Return ordered ``(value, label)`` toggle choices (presets + custom)."""
    choices: list[tuple[str, str]] = [
        (key, POST_UPDATE_PRESET_LABELS[key]) for key in POST_UPDATE_PRESETS
    ]
    choices.append(("custom", POST_UPDATE_PRESET_LABELS["custom"]))
    return choices


def get_post_update_targets(env: str | None = None) -> list[str]:
    """Return enabled post-update targets for ``env`` (``targets``, or legacy ``target``)."""
    env = env or config.settings.ENV
    cfg = config.settings.from_env(env).get("POST_UPDATE_LAUNCH", {})
    raw = cfg.get("targets")
    if raw is None:
        single = cfg.get("target")
        raw = [single] if single else []
    elif isinstance(raw, str):
        raw = [raw]

    result: list[str] = []
    for item in raw:
        value = str(item).strip().lower()
        if value and value != "none" and value not in result:
            result.append(value)
    return result


def _command_program(command: list[str] | str) -> str:
    """Return the program (first token) of a command list or string."""
    if isinstance(command, str):
        s = command.strip()
        if s.startswith('"'):
            return s[1:].partition('"')[0]
        return s.split(None, 1)[0] if s else ""
    return str(command[0]) if command else ""


def resolve_post_update_launch(
    env: str | None = None,
) -> list[LaunchCommand]:
    """Resolve enabled targets for ``env`` to launch commands, skipping unresolvable ones."""
    env = env or config.settings.ENV
    resolved: list[LaunchCommand] = []
    for target in get_post_update_targets(env):
        item = _resolve_launch_target(target, env)
        if item:
            resolved.append(item)
    return resolved


def resolve_post_update_launch_filtered(
    env: str | None = None,
    running: set[str] | None = None,
) -> FilteredLaunch:
    """Split the launch plan for ``env`` by which programs are already running."""
    from redfetch import processes

    if running is None:
        running = processes.running_executable_paths()
    to_run: list[LaunchCommand] = []
    skipped: list[str] = []
    for launch in resolve_post_update_launch(env):
        program = launch.program
        already_running = (
            program
            and os.path.isfile(program)
            and processes.is_executable_running(program, running)
        )
        if already_running:
            skipped.append(program)
        else:
            to_run.append(launch)
    return FilteredLaunch(to_run, skipped)


def _resolve_launch_target(
    target: str,
    env: str | None = None,
) -> LaunchCommand | None:
    """Resolve a single post-update ``target`` to a launch command."""
    env = env or config.settings.ENV
    cfg = config.settings.from_env(env).get("POST_UPDATE_LAUNCH", {})

    if target == "custom":
        command = cfg.get("command")
        if not command:
            print("Post-update launch is set to Custom, but no command is configured; skipping.")
            return None

        is_ps1 = (
            sys.platform == "win32"
            and _command_program(command).lower().endswith(".ps1")
        )

        if isinstance(command, str):
            command = command.strip()
            if not command:
                print("Post-update launch command is empty; skipping.")
                return None
            if sys.platform == "win32":
                if is_ps1:
                    command = (
                        "powershell -NoProfile -ExecutionPolicy Bypass -File " + command
                    )
                return LaunchCommand(command)
            return LaunchCommand(shlex.split(command, posix=True))

        if not isinstance(command, (list, tuple)):
            raise TypeError("POST_UPDATE_LAUNCH command must be a string or list.")
        argv = [str(part) for part in command]
        if not argv:
            print("Post-update launch command is empty; skipping.")
            return None
        if is_ps1:
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *argv]
        return LaunchCommand(argv)

    preset = POST_UPDATE_PRESETS.get(target)
    if not preset:
        print(f"Unknown POST_UPDATE_LAUNCH target: {target}; skipping.")
        return None
    folder = preset.resolve_dir()
    if folder and validate_file_in_path(folder, preset.executable):
        return LaunchCommand([os.path.join(folder, preset.executable)], folder)
    return None
