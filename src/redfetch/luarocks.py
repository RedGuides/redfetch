"""Pre-install MacroQuest's curated LuaRocks modules """

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

import httpx
import yaml

from redfetch import config, net
from redfetch.sync_types import SyncEventCallback
from redfetch.utils import get_vvmq_path


REPO_BASE_URL = "https://luarocks.macroquest.org"
MANIFEST_URL = f"{REPO_BASE_URL}/mq_luarocks.yaml"

LUA_VERSION = "5.1"

# Per-package subprocess. luarocks.exe normally finishes in seconds.
INSTALL_TIMEOUT_SECONDS = 180

# Printed by luarocks when a requested install is already present in the tree.
ALREADY_INSTALLED_MARKER = "already installed"

THREAD_URL = "https://www.redguides.com/community/threads/luarocks-packman-lsqlite3-not-working-correctly.93938/"
VC_REDIST_X86 = "https://aka.ms/vs/17/release/vc_redist.x86.exe"
VC_REDIST_X64 = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

MQ2LUA_DLL = os.path.join("plugins", "MQ2Lua.dll")
LUAJIT_VERSION_RE = re.compile(rb"LuaJIT\s+([0-9]+(?:\.[0-9A-Za-z-]+)+)")

# detect the MQ build's architecture
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664


@dataclass(frozen=True)
class Rock:
    """One rock from the MQ manifest."""
    name: str
    files: list[str] = field(default_factory=list)  # e.g. ["lfs.dll", "socket\\core.dll"]


def is_luarocks_enabled() -> bool:
    """Default-on: enabled unless LUAROCKS_ENABLED is explicitly set to False."""
    setting = config.settings.from_env(config.settings.ENV).get("LUAROCKS_ENABLED", None)
    return setting is not False


def _read_jit_version_from_mq2lua(vvmq_path: str) -> str | None:
    """Read the LuaJIT version embedded in MQ2Lua.dll."""
    dll_path = os.path.join(vvmq_path, MQ2LUA_DLL)
    try:
        with open(dll_path, "rb") as dll:
            data = dll.read()
    except OSError:
        return None

    match = LUAJIT_VERSION_RE.search(data)
    if not match:
        return None

    return match.group(1).decode("ascii", errors="ignore")


def _read_pe_machine(path: str) -> int | None:
    """Read the PE COFF machine type from a Windows binary, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            e_lfanew_bytes = f.read(4)
            if len(e_lfanew_bytes) != 4:
                return None
            f.seek(int.from_bytes(e_lfanew_bytes, "little"))
            if f.read(4) != b"PE\x00\x00":
                return None
            machine_bytes = f.read(2)
            if len(machine_bytes) != 2:
                return None
            return int.from_bytes(machine_bytes, "little")
    except OSError:
        return None


def _detect_mq_is_64bit(vvmq_path: str) -> bool | None:
    """Whether the MQ build is 64-bit, read from MQ2Lua.dll. None if undetectable."""
    machine = _read_pe_machine(os.path.join(vvmq_path, MQ2LUA_DLL))
    if machine == IMAGE_FILE_MACHINE_AMD64:
        return True
    if machine == IMAGE_FILE_MACHINE_I386:
        return False
    return None


def _vc_redist_url(vvmq_path: str) -> str:
    """Pick the VC++ redistributable matching the MQ build; default to x64 if unsure."""
    return VC_REDIST_X86 if _detect_mq_is_64bit(vvmq_path) is False else VC_REDIST_X64


def _find_luarocks_tree(vvmq_path: str) -> tuple[str, str] | None:
    """Resolve the active luarocks tree from the MQ2Lua.dll LuaJIT version."""
    jit_version = _read_jit_version_from_mq2lua(vvmq_path)
    if not jit_version:
        return None

    tree_path = os.path.join(vvmq_path, "modules", jit_version, "luarocks")
    return jit_version, tree_path


async def fetch_manifest() -> list[Rock] | None:
    """Fetch and parse the MQ rock manifest."""
    skip_msg = f"LuaRocks: skipping module bootstrap because {MANIFEST_URL} did not return a usable manifest."

    try:
        async with httpx.AsyncClient() as client:
            text = await net.get_text(client, MANIFEST_URL)
    except httpx.HTTPError as e:
        print(f"LuaRocks: could not fetch the module manifest: {e}")
        print(skip_msg)
        return None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        print(f"LuaRocks: could not parse the module manifest: {e}")
        print(skip_msg)
        return None

    return _parse_manifest(data)


def _parse_manifest(data: object) -> list[Rock]:
    """Turn the parsed YAML mapping into a list of Rock entries."""
    if not isinstance(data, dict):
        return []

    rocks: list[Rock] = []
    for name, info in data.items():
        if not isinstance(info, dict):
            info = {}
        files = [str(f) for f in (info.get("files") or [])]
        rocks.append(Rock(name=str(name), files=files))
    return rocks


def _file_in_tree(tree_path: str, rel: str) -> str:
    """Resolve a manifest file path (which may use backslashes) inside the tree's lib dir."""
    parts = rel.replace("\\", "/").split("/")
    return os.path.join(tree_path, "lib", "lua", LUA_VERSION, *parts)


def _rock_files_present(tree_path: str, rock: Rock) -> bool:
    """True if every DLL the rock should provide exists on disk."""
    if not rock.files:
        return False
    return all(os.path.isfile(_file_in_tree(tree_path, f)) for f in rock.files)


def _is_rock_satisfied(tree_path: str, rock: Rock) -> bool:
    """Whether a rock is already installed and can be skipped this run."""
    if rock.files:
        return _rock_files_present(tree_path, rock)
    # Pure-Lua rocks have no DLL to verify, so trust luarocks' own record
    recorded = os.path.join(tree_path, "lib", "luarocks", f"rocks-{LUA_VERSION}", rock.name)
    return os.path.isdir(recorded)


def _build_install_command(
    luarocks_exe: str,
    tree_path: str,
    jit_version: str,
    package: str,
) -> list[str]:
    """Mirror PackageMan.lua's install command line."""
    cache_path = os.path.join(tree_path, "cache")
    repo_url = f"{REPO_BASE_URL}/{jit_version}/"
    return [
        luarocks_exe,
        "--cache",
        cache_path,
        "--lua-version",
        LUA_VERSION,
        "--skip-config-warning",
        "--only-server",
        repo_url,
        "install",
        "--deps-mode",
        "none",
        "--tree",
        tree_path,
        package,
    ]


def _run_install(cmd: list[str], cwd: str) -> tuple[str, str]:
    """Run luarocks install and classify the result by its exit code."""
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW so users don't see a black cmd window flash.
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return "error", f"luarocks.exe timed out after {INSTALL_TIMEOUT_SECONDS}s\n{partial}"
    except OSError as exc:
        return "error", f"Failed to launch luarocks.exe: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        if ALREADY_INSTALLED_MARKER in output:
            return "skipped", output
        return "installed", output
    return "error", output


def _print_failure_hints(vvmq_path: str) -> None:
    """Print the same forum-derived hints we already give users in support."""
    print("  Hints:")
    print("    - If this keeps failing, ensure luarocks.macroquest.org isn't blocked by your router/ISP.")
    if _detect_mq_is_64bit(vvmq_path) is False:
        print(f"    - This MQ build is 32-bit; install the x86 Visual C++ redistributable: {VC_REDIST_X86}")
    else:
        print(f"    - This MQ build is 64-bit; install the x64 Visual C++ redistributable: {VC_REDIST_X64}")
    print("    - Set PackageMan.debug = true in lua/mq/PackageMan.lua for more detail.")
    print(f"    - Troubleshooting thread: {THREAD_URL}")


async def _install_rock(
    rock: Rock,
    *,
    luarocks_exe: str,
    tree_path: str,
    jit_version: str,
    cwd: str,
    on_event: SyncEventCallback | None,
) -> str:
    """Install one rock and verify it landed on disk."""
    if on_event:
        on_event(("start", rock.name, None))

    cmd = _build_install_command(luarocks_exe, tree_path, jit_version, rock.name)
    status, output = await asyncio.to_thread(_run_install, cmd, cwd)

    if status in {"installed", "skipped"} and rock.files and not _rock_files_present(tree_path, rock):
        status = "error"
        output = (
            f"{output}\nluarocks reported success but expected file(s) are "
            f"missing: {', '.join(rock.files)} (antivirus may have removed them)"
        )

    if status == "installed":
        print(f"LuaRocks: {rock.name} installed.")
        if on_event:
            on_event(("done", rock.name, "downloaded"))
        return "installed"

    if status == "skipped":
        if on_event:
            on_event(("done", rock.name, "skipped"))
        return "skipped"

    print(f"LuaRocks: {rock.name} failed to install.")
    print(f"  Command: {subprocess.list2cmdline(cmd)}")
    stripped = output.strip()
    if stripped:
        print("  Output:")
        for line in stripped.splitlines():
            print(f"    {line}")
    if on_event:
        on_event(("done", rock.name, "error"))
    return "error"


async def bootstrap_modules(
    on_event: SyncEventCallback | None = None,
) -> bool:
    """Pre-install the MQ-curated LuaRocks modules into the MQ modules tree."""
    if sys.platform != "win32":
        return True

    if not is_luarocks_enabled():
        return True

    vvmq_path = get_vvmq_path()
    if not vvmq_path:
        return True

    luarocks_exe = os.path.join(vvmq_path, "luarocks.exe")
    if not os.path.isfile(luarocks_exe):
        return True

    tree_info = _find_luarocks_tree(vvmq_path)
    if tree_info is None:
        return True

    jit_version, tree_path = tree_info
    try:
        os.makedirs(os.path.join(tree_path, "cache"), exist_ok=True)
    except OSError as exc:
        print(f"LuaRocks: could not prepare module tree: {exc}")
        return True

    rocks = await fetch_manifest()
    if not rocks:
        # Could not learn what to install; don't fail the sync over it.
        return True

    to_install = [rock for rock in rocks if not _is_rock_satisfied(tree_path, rock)]

    if not to_install:
        print("LuaRocks modules up-to-date.")
        return True

    if on_event:
        on_event(("add_total", len(to_install), None))

    print(f"LuaRocks: installing {len(to_install)} module(s); this can take several minutes...")

    installed = 0
    failed: list[str] = []

    # Serial on purpose: all rocks share one cache/tree, so parallel installs could race.
    for rock in to_install:
        status = await _install_rock(
            rock,
            luarocks_exe=luarocks_exe,
            tree_path=tree_path,
            jit_version=jit_version,
            cwd=vvmq_path,
            on_event=on_event,
        )
        if status == "installed":
            installed += 1
        elif status == "error":
            failed.append(rock.name)

    if failed:
        print(f"LuaRocks: {len(failed)} package(s) failed: {', '.join(failed)}")
        _print_failure_hints(vvmq_path)
        if on_event:
            on_event(("luarocks_failed", _vc_redist_url(vvmq_path), None))
        return False

    if installed:
        print(f"LuaRocks: installed {installed} module(s).")
    else:
        print("LuaRocks modules up-to-date.")

    return True
