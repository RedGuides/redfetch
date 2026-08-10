"""Provision a server's EverQuest folder from a clean RoF2 copy (zip, iso or folder)."""
# standard
import asyncio
import os
import re
import shutil
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from zipfile import BadZipFile, ZipFile

# third-party
import pycdlib
from rich.filesize import decimal
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

# local
from redfetch import config
from redfetch import detecteq
from redfetch import laa
from redfetch import patcher
from redfetch import servers
from redfetch import utils


# Work dirs need a name we recognize.
TEMP_PREFIX = ".redfetch-provision-"
_STALE_TEMP_AGE = 3600  # seconds

_COPY_CHUNK = 1024 * 1024
_TICK_SECONDS = 0.1  # a depot copy is ~10k chunks; repainting every one would drown the TUI

# Headroom on top of what the source actually holds.
FREE_SPACE_CUSHION = 1024 ** 3

# The clean source is a client-level thing, not a per-server one
SOURCE_ENV = "EMU"
SOURCE_KEY = "CLEAN_SOURCE"

NO_SOURCE_MESSAGE = (
    "Your clean archive of the EverQuest RoF2 client, such as a zip, iso, "
    "DVD or folder, is required for this feature."
)

# The finish chain below, disclosed before the user starts it.
STEPS_NOTE = (
    "Copies the source files into the new folder, downloads the server's "
    "patcher into it (if specified), then sets eqgame.exe to use 4GB of memory."
)

# Reported once the folder has landed, so callers know cancelling is no longer possible.
FINISHING_LABEL = "Finishing setup"

EQGAME = "eqgame.exe"

_DAMAGED_ISO = "{name} is damaged: it's shorter than it says it is."

# What this Python's zipfile can decompress
_ZIP_METHODS = frozenset(
    getattr(zipfile, name)
    for name in ("ZIP_STORED", "ZIP_DEFLATED", "ZIP_BZIP2", "ZIP_LZMA", "ZIP_ZSTANDARD")
    if hasattr(zipfile, name)
)

_SEPARATORS = re.compile(r"[/\\]")

# fraction is None while a step can't be measured.
ProgressCallback = Callable[[str, float | None], None]


class ProvisionError(Exception):
    """A failure whose message is written for the user."""


class ProvisionCancelled(ProvisionError):
    """The user backed out. A notice, not an error."""


class SourceKind(Enum):
    ZIP = auto()
    ISO = auto()
    TREE = auto()


@dataclass(frozen=True, slots=True)
class SourcePlan:
    """What a scan of the source found, and what copying it would cost."""
    kind: SourceKind
    root: tuple[str, ...] | Path  # path inside archive or the folder on disk that holds eqgame.exe
    total_bytes: int
    file_count: int


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """Where the new folder landed, and anything the user should know about it."""
    destination: Path
    notices: tuple[str, ...] = ()


#
# the clean source setting
#

def clean_source() -> str:
    """The remembered clean RoF2 source, or ''."""
    return str(config.settings.from_env(SOURCE_ENV).get(SOURCE_KEY) or "")


def set_clean_source(path: str | None) -> None:
    """Remember a clean source, refusing one that's also serving a server."""
    value = str(path or "").strip()
    if value:
        conflicts = clean_source_conflicts(value)
        if conflicts:
            raise ProvisionError(
                f"That folder is {conflicts[0]}, so it isn't a clean copy. Playing on "
                "a server patches its folder. Point redfetch at your untouched RoF2 copy."
            )
    config.update_setting([SOURCE_KEY], value or None, env=SOURCE_ENV)


def clean_source_conflicts(path: str) -> list[str]:
    """Server folders that overlap *path*, described for a refusal message."""
    conflicts = []
    for env in config.MULTI_SERVER_ENVS:
        if utils.paths_overlap(path, str(config.settings.from_env(env).get("EQPATH") or "")):
            conflicts.append(f"the EverQuest folder in use on {config.ENVS[env]}")
        for slug, entry in servers.list_servers(env).items():
            if utils.paths_overlap(path, str(entry.get("eqpath") or "")):
                conflicts.append(f"the EverQuest folder for {entry.get('label') or slug}")
        if utils.paths_overlap(path, servers.generic_eqpath(env)):
            conflicts.append(f"the EverQuest folder for {config.BARE_SERVER_LABEL}")
    return conflicts


#
# reading the source
#

def classify_source(source: str) -> SourceKind:
    """Decide by shape and extension, never by sniffing."""
    path = str(source or "").strip()
    if not path:
        raise ProvisionError(NO_SOURCE_MESSAGE)
    if os.path.isdir(path):
        return SourceKind.TREE
    if not os.path.exists(path):
        raise ProvisionError(f"Your clean RoF2 copy isn't there anymore: {path}")
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".zip":
        return SourceKind.ZIP
    if suffix == ".iso":
        return SourceKind.ISO
    raise ProvisionError(
        f"redfetch can't read {os.path.basename(path)} as a clean RoF2 copy: "
        "it takes a .zip, an .iso, or a folder."
    )


def _member_parts(name: str) -> tuple[str, ...]:
    """Split a zip member or a path on a disc, on either separator."""
    return tuple(part for part in _SEPARATORS.split(name) if part and part != ".")


def _safe_parts(name: str) -> tuple[str, ...]:
    """Member parts, refusing any that would write outside the destination."""
    parts = _member_parts(name)
    if any(part == ".." or ":" in part for part in parts):
        raise ProvisionError(
            f"This archive holds a file name that would write outside the new folder: {name}"
        )
    return parts


def _fold(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part.lower() for part in parts)


def _under(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """True when a member sits inside the winning root, by Windows' naming rules."""
    return len(parts) > len(prefix) and _fold(parts[:len(prefix)]) == _fold(prefix)


def _require_one_root[Root](found: list[tuple[Root, str]], source) -> Root:
    """Exactly one EverQuest folder."""
    if not found:
        raise ProvisionError(
            f"There's no {EQGAME} anywhere in {source}, so it isn't a RoF2 copy."
        )
    if len(found) > 1:
        listed = "\n".join(f"  {label}" for _value, label in found)
        raise ProvisionError(
            f"{source} holds more than one EverQuest folder, so redfetch doesn't know "
            f"which one to copy:\n{listed}"
        )
    return found[0][0]


def context_for(slug: str, env: str, *, eqpath: str, label: str = "",
                patcher_url: str = "", patcher_exe: str = "") -> servers.ServerContext:
    """A known server's own settings, or the ones the caller collected for a new custom one."""
    if slug in servers.list_servers(env):
        return servers.server_context(slug, env, eqpath=eqpath)
    try:
        # add_server checks this too, but only after the copy, and it raises ValueError.
        servers.validate_server_slug(slug, must_be_new=True)
    except ValueError as exc:
        raise ProvisionError(str(exc)) from exc
    return servers.ServerContext(
        label=label or slug,
        eqpath=eqpath,
        patcher_url=patcher_url,
        patcher_exe=patcher_exe,
    )


def scan_source(source: Path, kind: SourceKind) -> SourcePlan:
    """Find the EverQuest folder inside the source and measure what we'd copy."""
    if kind is SourceKind.ZIP:
        return _scan_zip(source)
    if kind is SourceKind.ISO:
        return _scan_iso(source)
    return _scan_tree(source)


def _scan_zip(source: Path) -> SourcePlan:
    try:
        with ZipFile(source) as archive:
            members = [(_member_parts(info.filename), info) for info in archive.infolist()]
    except (BadZipFile, OSError) as exc:
        raise ProvisionError(f"Couldn't read {source.name}: {exc}") from exc

    roots: dict[tuple[str, ...], tuple[str, ...]] = {}
    for parts, info in members:
        if info.is_dir() or not parts or parts[-1].lower() != EQGAME:
            continue
        roots.setdefault(_fold(parts[:-1]), parts[:-1])
    found = [
        (roots[key], "/".join(roots[key]) or "(the top level of the archive)")
        for key in sorted(roots)
    ]
    prefix = _require_one_root(found, source)

    files = [info for parts, info in members if not info.is_dir() and _under(parts, prefix)]
    for info in files:  # only what we'd copy
        if info.flag_bits & 0x1:
            raise ProvisionError(
                f"{source.name} is password-protected, so redfetch can't copy from it."
            )
        if info.compress_type not in _ZIP_METHODS:
            method = zipfile.compressor_names.get(info.compress_type, f"method {info.compress_type}")
            raise ProvisionError(
                f"{source.name} is compressed with {method}, which redfetch can't read."
            )
    return SourcePlan(SourceKind.ZIP, prefix, sum(info.file_size for info in files), len(files))


def _iso_name(name: str) -> str:
    """Strip the ISO9660 version suffix."""
    stem = name.split(";", 1)[0]
    return stem.removesuffix(".") or stem


def _iso_path(dirpath: str, name: str) -> str:
    return f"{dirpath.rstrip('/')}/{name}"


@contextmanager
def _open_iso(source: Path):
    image = pycdlib.PyCdlib()
    image.open(str(source))
    try:
        if image.has_udf():
            yield image, image.get_udf_facade()
        elif image.has_joliet():
            yield image, image.get_joliet_facade()
        elif image.has_rock_ridge():
            yield image, image.get_rock_ridge_facade()
        else:
            yield image, image.get_iso9660_facade()
    finally:
        image.close()


def _refuse_if_short(source: Path, image) -> None:
    """Catch a truncated ISO."""
    declared = image.pvd.space_size * image.pvd.logical_block_size()
    if declared > source.stat().st_size:
        raise ProvisionError(_DAMAGED_ISO.format(name=source.name))


def _scan_iso(source: Path) -> SourcePlan:
    try:
        with _open_iso(source) as (image, facade):
            _refuse_if_short(source, image)
            walked = [(dirpath, filenames) for dirpath, _dirs, filenames in facade.walk("/")]
            roots: dict[tuple[str, ...], tuple[str, ...]] = {}
            for dirpath, filenames in walked:
                if any(_iso_name(name).lower() == EQGAME for name in filenames):
                    parts = _member_parts(dirpath)
                    roots.setdefault(_fold(parts), parts)
            found = [
                (roots[key], "/".join(roots[key]) or "(the top level of the disc)")
                for key in sorted(roots)
            ]
            prefix = _require_one_root(found, source)

            total = 0
            count = 0
            for dirpath, filenames in walked:
                base = _member_parts(dirpath)
                for name in filenames:
                    if not _under(base + (name,), prefix):
                        continue
                    length = facade.get_record(_iso_path(dirpath, name)).get_data_length()
                    if length < 0: 
                        raise ProvisionError(_DAMAGED_ISO.format(name=source.name))
                    total += length
                    count += 1
    except ProvisionError:
        raise
    except Exception as exc:
        raise ProvisionError(f"Couldn't read {source.name}: {exc}") from exc
    return SourcePlan(SourceKind.ISO, prefix, total, count)


def _scan_tree(source: Path) -> SourcePlan:
    # Never hunt through subfolders if the user pointed at a folder
    if not detecteq.is_valid_eq_dir(str(source)):
        raise ProvisionError(
            f"There's no {EQGAME} in {source}, so it isn't a RoF2 copy. "
            f"Point redfetch at the folder that holds {EQGAME} itself."
        )
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in source.walk():
        for name in filenames:
            try:
                total += (dirpath / name).stat().st_size
            except OSError:
                continue
            count += 1
    return SourcePlan(SourceKind.TREE, source, total, count)


#
# checks before anything is written
#

def validate_destination(destination: Path, source: Path) -> None:
    """Absent or empty only, and never tangled up with the clean source."""
    if utils.paths_overlap(str(source), str(destination)):
        raise ProvisionError(
            "The new folder can't be inside your clean RoF2 copy, or the other way "
            "around. Pick somewhere else."
        )
    if not destination.exists():
        return
    if not destination.is_dir():
        raise ProvisionError(f"{destination} is a file, so it can't be an EverQuest folder.")
    try:
        occupied = any(destination.iterdir())
    except OSError as exc:
        raise ProvisionError(f"Couldn't read {destination}: {exc}") from exc
    if occupied:
        raise ProvisionError(
            f"{destination} already has files in it. If that's an EverQuest install, add "
            'it with "use an existing folder" instead.'
        )


def check_free_space(plan: SourcePlan, destination: Path) -> None:
    """Refuse before copying when the volume can't hold the result."""
    needed = plan.total_bytes + FREE_SPACE_CUSHION
    # materialize creates the parent chain later; free space is a property of the
    # volume, so ask the nearest folder that's actually there.
    parent = _nearest_existing(destination.parent)
    try:
        free = shutil.disk_usage(parent).free
    except OSError as exc:
        raise ProvisionError(f"Couldn't check the free space in {parent}: {exc}") from exc
    if free < needed:
        raise ProvisionError(
            f"There isn't enough room in {parent}: this needs {decimal(needed)} "
            f"free, and only {decimal(free)} is available."
        )


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


#
# materializing
#

def materialize(source: Path, plan: SourcePlan, destination: Path, *,
                progress: ProgressCallback | None = None,
                cancelled: Callable[[], bool] | None = None) -> None:
    """Copy the source's EverQuest folder into place, all or nothing.

    Cleanup must stay in this thread.
    """
    tick = _ticker(plan.total_bytes, progress)
    is_cancelled = cancelled or (lambda: False)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProvisionError(f"Couldn't create {destination.parent}: {exc}") from exc
    utils.sweep_stale_dirs(str(destination.parent), TEMP_PREFIX, _STALE_TEMP_AGE)
    try:
        work_dir = Path(tempfile.mkdtemp(dir=destination.parent, prefix=TEMP_PREFIX))
    except OSError as exc:
        raise ProvisionError(f"Couldn't write to {destination.parent}: {exc}") from exc

    try:
        if plan.kind is SourceKind.ZIP:
            _copy_from_zip(source, plan, work_dir, tick, is_cancelled)
        elif plan.kind is SourceKind.ISO:
            _copy_from_iso(source, plan, work_dir, tick, is_cancelled)
        else:
            _copy_from_tree(plan, work_dir, tick, is_cancelled)
        _swap_into_place(work_dir, destination)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _ticker(total: int, progress: ProgressCallback | None):
    """Byte-counting progress, shared by both copy routes."""
    done = 0
    last = 0.0

    def tick(label: str, count: int) -> None:
        nonlocal done, last
        done += count
        if not progress:
            return
        now = time.monotonic()
        if done < total and now - last < _TICK_SECONDS:
            return
        last = now
        progress(label, min(done / total, 1.0) if total else None)

    return tick


def _copy_stream(reader, writer, label: str, tick, is_cancelled) -> int:
    copied = 0
    while True:
        if is_cancelled():
            raise ProvisionCancelled("Setup was cancelled, so nothing was created.")
        chunk = reader.read(_COPY_CHUNK)
        if not chunk:
            return copied
        writer.write(chunk)
        copied += len(chunk)
        tick(label, len(chunk))


def _copy_from_zip(source: Path, plan: SourcePlan, work_dir: Path, tick, is_cancelled) -> None:
    prefix = plan.root
    try:
        with ZipFile(source) as archive:
            for info in archive.infolist():
                parts = _member_parts(info.filename)
                if not _under(parts, prefix):
                    continue  # a self-packaged source can hold anything alongside the game
                relative = _safe_parts(info.filename)[len(prefix):]
                target = work_dir.joinpath(*relative)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as reader, open(target, "wb") as writer:
                    _copy_stream(reader, writer, relative[-1], tick, is_cancelled)
    except (BadZipFile, zlib.error) as exc:
        raise ProvisionError(f"{source.name} is damaged, so nothing was created: {exc}") from exc
    except (OSError, RuntimeError, NotImplementedError) as exc:
        raise ProvisionError(f"Couldn't copy from {source.name}: {exc}") from exc


def _copy_from_iso(source: Path, plan: SourcePlan, work_dir: Path, tick, is_cancelled) -> None:
    prefix = plan.root
    try:
        with _open_iso(source) as (_image, facade):
            for dirpath, _dirs, filenames in facade.walk("/"):
                base = _member_parts(dirpath)
                if not (_fold(base) == _fold(prefix) or _under(base, prefix)):
                    continue  # a self-packaged disc can hold anything alongside the game
                # Every directory is walked
                target_dir = work_dir.joinpath(*_safe_parts(dirpath)[len(prefix):])
                target_dir.mkdir(parents=True, exist_ok=True)
                for name in sorted(filenames):
                    full = _iso_path(dirpath, name)
                    leaf = _iso_name(_safe_parts(full)[-1])
                    length = facade.get_record(full).get_data_length()
                    with facade.open_file_from_iso(full) as reader, \
                            open(target_dir / leaf, "wb") as writer:
                        copied = _copy_stream(reader, writer, leaf, tick, is_cancelled)
                    if copied != length:
                        raise ProvisionError(_DAMAGED_ISO.format(name=source.name))
    except ProvisionError:
        raise # including user-cancelled
    except Exception as exc:
        raise ProvisionError(f"Couldn't copy from {source.name}: {exc}") from exc


def _copy_from_tree(plan: SourcePlan, work_dir: Path, tick, is_cancelled) -> None:
    root = plan.root
    try:
        for dirpath, _dirnames, filenames in root.walk():
            target_dir = work_dir / dirpath.relative_to(root)
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in sorted(filenames):
                # Content only, never copystat: a DVD source would make everything read-only.
                with open(dirpath / name, "rb") as reader, \
                        open(target_dir / name, "wb") as writer:
                    _copy_stream(reader, writer, name, tick, is_cancelled)
    except OSError as exc:
        raise ProvisionError(f"Couldn't copy from {root}: {exc}") from exc


@retry(
    retry=retry_if_exception_type(PermissionError),
    stop=stop_after_attempt(10),
    wait=wait_fixed(0.3),
    reraise=True,
)
def _rename_with_retry(work_dir: Path, destination: Path) -> None:
    # Windows refuses a directory rename sometimes, so retry a few times before giving up.
    os.rename(work_dir, destination)


def _swap_into_place(work_dir: Path, destination: Path) -> None:
    """The one moment the destination exists: an empty folder gives way to a rename."""
    try:
        if destination.exists():
            os.rmdir(destination)
        _rename_with_retry(work_dir, destination)
    except OSError as exc:
        raise ProvisionError(f"Couldn't move the new folder into {destination}: {exc}") from exc


#
# the finish chain
#

async def provision(slug: str, *, env: str, source: str, destination: str,
                    label: str = "", patcher_url: str = "", patcher_exe: str = "",
                    progress: ProgressCallback | None = None,
                    cancelled: Callable[[], bool] | None = None) -> ProvisionResult:
    """Materialize a server's folder from a clean source, set it up, and register it.

    Failure before the folder lands leaves nothing. Failure after the folder lands
    are just warnings, since user can always run each step via the TUI or CLI.
    """
    raw_source = str(source or "").strip()
    raw_dest = str(destination or "").strip()
    dest_path = Path(raw_dest)
    if not raw_dest or str(dest_path) == ".":  # "." would provision over the working directory
        raise ProvisionError("The new EverQuest folder needs a destination.")

    kind = classify_source(raw_source)  # refuses an empty source before any path is walked
    source_path = Path(raw_source)
    ctx = context_for(
        slug, env, eqpath=str(dest_path), label=label, patcher_url=patcher_url,
        patcher_exe=patcher_exe,
    )
    validate_destination(dest_path, source_path)

    _report(progress, f"Reading {source_path.name or source_path}")
    # Off the event loop: a DVD-drive source stats thousands of files, slowly.
    plan = await asyncio.to_thread(scan_source, source_path, kind)
    check_free_space(plan, dest_path)

    await asyncio.to_thread(
        materialize, source_path, plan, dest_path, progress=progress, cancelled=cancelled
    )
    # The folder has landed: from here every failure is a warning, and there's
    # nothing left to cancel.
    _report(progress, FINISHING_LABEL)

    notices: list[str] = []
    if patcher.has_patcher(ctx):
        _report(progress, f"Installing the {ctx.label} patcher")
        try:
            await patcher.install(ctx)
        except patcher.PatcherError as exc:
            notices.append(f"Couldn't install the {ctx.label} patcher: {exc}")

    # After the patcher, not before, since it may contain its own eqgame.exe 
    _report(progress, "Turning on the 4GB allowance")
    try:
        await asyncio.to_thread(laa.enable, str(dest_path))
    except laa.LaaError as exc:
        notices.append(f"Couldn't turn on the 4GB allowance: {exc}")

    if utils.dx9_notice_wanted():
        notices.append(
            "DirectX 9 wasn't detected on your computer, which EverQuest needs. "
            f"Get it from Microsoft: {utils.DX9_INSTALLER_URL}"
        )

    if not detecteq.is_valid_eq_dir(str(dest_path)):
        # Should be impossible, but you never know.
        raise ProvisionError(
            f"{dest_path} has no {EQGAME} after copying, so it wasn't added as a server."
        )
    servers.add_server(
        slug, env=env, eqpath=str(dest_path), label=label, patcher_url=patcher_url,
        patcher_exe=patcher_exe,
    )
    return ProvisionResult(dest_path, tuple(notices))


def _report(progress: ProgressCallback | None, label: str) -> None:
    if progress:
        progress(label, None)


def default_destination(slug: str) -> str:
    """Where a provision lands unless the user says otherwise."""
    download_folder = str(config.settings.from_env(SOURCE_ENV).get("DOWNLOAD_FOLDER") or "")
    return os.path.normpath(os.path.join(download_folder, f"EverQuest_{slug}"))
