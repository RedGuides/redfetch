"""Helpers for checking and installing common MacroQuest LuaRocks packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys


MQ_LUAROCKS_REPO = "https://luarocks.macroquest.org/"
MQ_LUAROCKS_LUA_VERSION = "5.1"
LUAROCKS_INSTALL_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class CommonLuaPackage:
    package_name: str
    require_name: str


COMMON_LUA_PACKAGES = (
    CommonLuaPackage("lsqlite3", "lsqlite3"),
    CommonLuaPackage("luafilesystem", "lfs"),
)


@dataclass(frozen=True)
class LuaPackageStatus:
    package_name: str
    require_name: str
    installed: bool
    install_attempted: bool = False
    install_succeeded: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class LuaFixResult:
    mq_path: Path
    luarocks_exe: Path | None
    target_tree: Path | None
    statuses: tuple[LuaPackageStatus, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(status.installed for status in self.statuses)


def _iter_luarocks_trees(modules_dir: Path) -> list[Path]:
    trees: list[Path] = []

    top_level = modules_dir / "luarocks"
    if top_level.is_dir():
        trees.append(top_level)

    for child in modules_dir.iterdir():
        if not child.is_dir():
            continue
        candidate = child / "luarocks"
        if candidate.is_dir():
            trees.append(candidate)

    return trees


def _tree_version_tokens(tree: Path) -> tuple[int, ...] | None:
    tokens = tree.parent.name.replace("-", ".").split(".")
    if not tokens or not all(token.isdigit() for token in tokens):
        return None
    return tuple(int(token) for token in tokens)


def _tree_sort_key(tree: Path) -> tuple[int, ...]:
    return _tree_version_tokens(tree) or ()


def find_luarocks_executable(mq_path: str | Path) -> Path | None:
    mq_root = Path(mq_path)
    candidate = mq_root / "luarocks.exe"
    return candidate if candidate.is_file() else None


def find_luarocks_tree(mq_path: str | Path) -> Path | None:
    modules_dir = Path(mq_path) / "modules"
    if not modules_dir.is_dir():
        return None

    trees = _iter_luarocks_trees(modules_dir)
    if not trees:
        return None

    versioned = [
        tree
        for tree in trees
        if tree.parent != modules_dir and _tree_version_tokens(tree) is not None
    ]
    if versioned:
        versioned.sort(key=_tree_sort_key)
        return versioned[-1]

    return trees[0]


def _repo_for_tree(tree: Path) -> str:
    jit_version = tree.parent.name
    if jit_version and jit_version != "modules":
        return f"{MQ_LUAROCKS_REPO}{jit_version}/"
    return MQ_LUAROCKS_REPO


def _module_exists(tree: Path, require_name: str) -> bool:
    lua_dir = tree / "lib" / "lua" / MQ_LUAROCKS_LUA_VERSION
    if not lua_dir.is_dir():
        return False

    if require_name == "lfs":
        patterns = ("lfs.dll", "lfs.lua")
    else:
        patterns = (
            f"{require_name}.dll",
            f"{require_name}.lua",
            f"{require_name}.so",
            f"{require_name}.rockspec",
        )

    for pattern in patterns:
        if (lua_dir / pattern).exists():
            return True

    # Some rocks ship nested modules rather than a single file.
    if (lua_dir / require_name).exists():
        return True

    return False


def check_common_lua_packages(tree: Path) -> tuple[LuaPackageStatus, ...]:
    return tuple(
        LuaPackageStatus(
            package_name=package.package_name,
            require_name=package.require_name,
            installed=_module_exists(tree, package.require_name),
        )
        for package in COMMON_LUA_PACKAGES
    )


def _package_install_command(luarocks_exe: Path, tree: Path, package_name: str) -> list[str]:
    cache_dir = tree / "cache"
    return [
        str(luarocks_exe),
        "--cache",
        str(cache_dir),
        "--lua-version",
        MQ_LUAROCKS_LUA_VERSION,
        "--skip-config-warning",
        "--only-server",
        _repo_for_tree(tree),
        "install",
        "--deps-mode",
        "none",
        "--tree",
        str(tree),
        package_name,
    ]


def describe_status(status: LuaPackageStatus) -> str:
    if status.install_attempted:
        return "installed and verified" if status.install_succeeded else "install failed verification"
    return "already present and verified" if status.installed else "missing"


def _format_process_output(*parts: object) -> str | None:
    output_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        text = part.decode(errors="replace") if isinstance(part, bytes) else str(part)
        if text.strip():
            output_parts.append(text.strip())
    return "\n".join(output_parts) or None


def _failed_install_status(package: CommonLuaPackage, exc: Exception) -> LuaPackageStatus:
    detail_bits = [f"{type(exc).__name__}: {exc}"]
    output = _format_process_output(
        getattr(exc, "stdout", None),
        getattr(exc, "stderr", None),
    )
    if output:
        detail_bits.append(output)
    return LuaPackageStatus(
        package_name=package.package_name,
        require_name=package.require_name,
        installed=False,
        install_attempted=True,
        install_succeeded=False,
        detail="\n".join(detail_bits),
    )


def install_common_lua_package(
    *,
    luarocks_exe: Path,
    tree: Path,
    package: CommonLuaPackage,
) -> LuaPackageStatus:
    command = _package_install_command(luarocks_exe, tree, package.package_name)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=LUAROCKS_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _failed_install_status(package, exc)
    except Exception as exc:
        return _failed_install_status(package, exc)

    installed = _module_exists(tree, package.require_name)
    detail_bits: list[str] = []
    if completed.returncode != 0:
        detail_bits.append(f"luarocks exited with code {completed.returncode}")
    output = _format_process_output(completed.stdout, completed.stderr)
    if output:
        detail_bits.append(output)

    return LuaPackageStatus(
        package_name=package.package_name,
        require_name=package.require_name,
        installed=installed,
        install_attempted=True,
        install_succeeded=installed,
        detail="\n".join(detail_bits) if detail_bits else None,
    )


def ensure_common_lua_packages(mq_path: str | Path) -> LuaFixResult:
    mq_root = Path(mq_path)

    if sys.platform != "win32":
        return LuaFixResult(
            mq_path=mq_root,
            luarocks_exe=None,
            target_tree=None,
            statuses=(),
            error="LuaRocks package repair is currently supported only on Windows.",
        )

    if not mq_root.is_dir():
        return LuaFixResult(
            mq_path=mq_root,
            luarocks_exe=None,
            target_tree=None,
            statuses=(),
            error=f"MacroQuest path not found: {mq_root}",
        )

    luarocks_exe = find_luarocks_executable(mq_root)
    if luarocks_exe is None:
        return LuaFixResult(
            mq_path=mq_root,
            luarocks_exe=None,
            target_tree=None,
            statuses=(),
            error=f"luarocks.exe not found in {mq_root}",
        )

    tree = find_luarocks_tree(mq_root)
    if tree is None:
        return LuaFixResult(
            mq_path=mq_root,
            luarocks_exe=luarocks_exe,
            target_tree=None,
            statuses=(),
            error=f"No LuaRocks tree found under {mq_root / 'modules'}",
        )

    initial_statuses = check_common_lua_packages(tree)
    final_statuses: list[LuaPackageStatus] = []
    for initial_status, package in zip(initial_statuses, COMMON_LUA_PACKAGES):
        if initial_status.installed:
            final_statuses.append(initial_status)
            continue

        final_statuses.append(
            install_common_lua_package(
                luarocks_exe=luarocks_exe,
                tree=tree,
                package=package,
            )
        )

    return LuaFixResult(
        mq_path=mq_root,
        luarocks_exe=luarocks_exe,
        target_tree=tree,
        statuses=tuple(final_statuses),
    )
