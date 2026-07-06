"""Shared shortcut registry for the TUI and CLI."""
from __future__ import annotations

import os
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from redfetch import config
from redfetch import processes
from redfetch import utils


# ---- resolvers -------------------------------------------------------------
# Resolve paths lazily so --server can switch environments before launch.

def _eq_dir() -> str | None:
    return config.settings.from_env(config.settings.ENV).get("EQPATH") or None


def _vvmq_config_dir() -> str | None:
    """MacroQuest config folder."""
    vvmq = utils.get_vvmq_path()
    return os.path.join(vvmq, "config") if vvmq else None


def _redfetch_config_dir() -> str | None:
    return config.config_dir


def _ensure_redfetch_config() -> None:
    """Ensure settings.local.toml exists."""
    config.ensure_config_file_exists(
        os.path.join(config.config_dir, "settings.local.toml")
    )


def _seed_meshgen_ini() -> None:
    """Seed MeshGenerator's first-run paths if needed and possible."""
    if os.name != "nt":
        return
    vvmq = utils.get_vvmq_path()  # also the folder MeshGenerator.exe launches from
    if not vvmq or not os.path.isdir(vvmq):
        return

    import ctypes
    from ctypes import wintypes

    try:
        ini = os.path.join(vvmq, "config", "MeshGenerator.ini")
        os.makedirs(os.path.dirname(ini), exist_ok=True)

        k32 = ctypes.windll.kernel32
        k32.GetPrivateProfileStringW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPWSTR, wintypes.DWORD, wintypes.LPCWSTR,
        ]
        k32.GetPrivateProfileStringW.restype = wintypes.DWORD
        k32.WritePrivateProfileStringW.argtypes = [wintypes.LPCWSTR] * 4
        k32.WritePrivateProfileStringW.restype = wintypes.BOOL

        def _seed_if_empty(key: str, value: str | None) -> None:
            if not value:
                return
            buf = ctypes.create_unicode_buffer(512)
            k32.GetPrivateProfileStringW("General", key, "", buf, len(buf), ini)
            if not buf.value:
                k32.WritePrivateProfileStringW("General", key, value, ini)

        _seed_if_empty("Output Path", vvmq)         # MQ folder = mesh output root
        _seed_if_empty("EverQuest Path", _eq_dir())
    except OSError:
        pass  # unwritable config dir, etc. — MeshGenerator will just prompt as before


# ---- executables: `redfetch run <key>` -------------------------------------

@dataclass(frozen=True)
class Runnable:
    key: str
    label: str                                  # TUI label
    executable: str
    resolve_dir: Callable[[], str | None]
    args: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    tooltip: str = ""
    prepare: Callable[[], None] | None = None   # optional pre-launch hook


RUNNABLES: tuple[Runnable, ...] = (
    Runnable(
        "vvmq", "Very Vanilla MQ 🍦", "MacroQuest.exe", utils.get_vvmq_path,
        aliases=("mq", "macroquest"),
        tooltip="Run MacroQuest, the legendary add-on platform for EverQuest.",
    ),
    Runnable(
        "meshgenerator", "MeshGenerator 🌐", "MeshGenerator.exe", utils.get_vvmq_path,
        aliases=("mesh", "meshgen"),
        tooltip="Generate your own EQ zone navmeshes for MQNav.",
        prepare=_seed_meshgen_ini,
    ),
    Runnable(
        "eqbcs", "EQBCS 💬", "EQBCS.exe", utils.get_vvmq_path,
        aliases=("bcs",),
        tooltip="run EQBCs.exe, the server for EQ Box Chat (MQ2EQBC).",
    ),
    Runnable(
        "launchpad", "EQ LaunchPad 🐲", "LaunchPad.exe", _eq_dir,
        aliases=("eqlp", "eq"),
        tooltip="The official launcher and updater for EverQuest.",
    ),
    Runnable(
        "eqgame", "EQGame 🐲🩹", "eqgame.exe", _eq_dir, args=("patchme",),
        aliases=("eqclient",),
        tooltip="The EverQuest client *WITHOUT* updating.",
    ),
    Runnable(
        "myseq", "MySEQ 📍", "MySEQ.exe", utils.get_myseq_path,
        aliases=("seq",),
        tooltip="run MySEQ.exe, a real-time map viewer for EverQuest.",
    ),
)


# ---- folders & files: `redfetch open <key>` --------------------------------

@dataclass(frozen=True)
class Openable:
    key: str
    label: str
    resolve_dir: Callable[[], str | None]
    filename: str | None = None                 # None opens the folder
    aliases: tuple[str, ...] = ()
    tooltip: str = ""
    prepare: Callable[[], None] | None = None   # optional pre-open hook
    css: str = "folder"                         # TUI class


OPENABLES: tuple[Openable, ...] = (
    # folders
    Openable(
        "downloads", "Downloads 📦", utils.get_current_download_folder,
        aliases=("dl",), tooltip="Open redfetch downloads folder",
    ),
    Openable(
        "vvmq", "Very Vanilla MQ 🍦", utils.get_vvmq_path,
        aliases=("mq",), tooltip="Open MacroQuest folder",
    ),
    Openable(
        "eq", "EverQuest 🐲", _eq_dir,
        tooltip="Open EverQuest game folder",
    ),
    Openable(
        "myseq", "MySEQ 📍", utils.get_myseq_path,
        aliases=("seq",), tooltip="Open MySEQ folder",
    ),
    # files
    Openable(
        "config", "settings.local.toml 📦", _redfetch_config_dir, "settings.local.toml",
        aliases=("settings",), css="file", prepare=_ensure_redfetch_config,
        tooltip="Open the redfetch config file.",
    ),
    Openable(
        "mq-config", "MacroQuest.ini 🍦", _vvmq_config_dir, "MacroQuest.ini",
        aliases=("mqini",), css="file", tooltip="Open VV MQ's config file.",
    ),
    Openable(
        "eq-config", "eqclient.ini 🐲", _eq_dir, "eqclient.ini",
        css="file", tooltip="Open EverQuest's config file.",
    ),
    Openable(
        "eqhost", "eqhost.txt 🐲", _eq_dir, "eqhost.txt",
        css="file", tooltip="Open EverQuest's eqhost.txt, useful for emulators.",
    ),
)


# ---- lookup ----------------------------------------------------------------

_RUN_BY_NAME: dict[str, Runnable] = {
    name: r for r in RUNNABLES for name in (r.key, *r.aliases)
}
_OPEN_BY_NAME: dict[str, Openable] = {
    name: o for o in OPENABLES for name in (o.key, *o.aliases)
}


def find_runnable(name: str) -> Runnable | None:
    return _RUN_BY_NAME.get(name.strip().lower())


def find_openable(name: str) -> Openable | None:
    return _OPEN_BY_NAME.get(name.strip().lower())


# ---- availability (drives TUI disable + CLI listing) -----------------------

def runnable_available(r: Runnable) -> bool:
    return utils.validate_file_in_path(r.resolve_dir(), r.executable)


def openable_available(o: Openable) -> bool:
    folder = o.resolve_dir()
    if not folder:
        return False
    if o.filename:
        return utils.validate_file_in_path(folder, o.filename)
    return os.path.isdir(folder)


# ---- launch (the single execution point both front-ends call) --------------

def run(r: Runnable, extra: Sequence[str] | None = None) -> None:
    """Launch a registered executable."""
    if r.prepare:
        r.prepare()
    processes.run_executable(r.resolve_dir(), r.executable, [*r.args, *(extra or [])])


def open_target(o: Openable) -> str:
    """Open a registered folder or file."""
    folder = o.resolve_dir()
    if not folder:
        raise FileNotFoundError(f"Path not set for {o.key!r}.")
    if o.prepare:
        o.prepare()
    if o.filename is None:
        processes.open_folder(folder)
        return ""
    return processes.open_file(folder, o.filename)
