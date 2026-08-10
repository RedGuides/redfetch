"""Download a server's own patcher program."""
# standard
import asyncio
import os
import shutil
import tempfile
import zlib
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

# third-party
import httpx
from pathvalidate import ValidationError, validate_filename

# local
from redfetch import download
from redfetch import net
from redfetch import utils
from redfetch.servers import ServerContext


# Work dirs live in the EQ folder, so they need a name we can recognize as ours.
TEMP_PREFIX = ".redfetch-patcher-"
_STALE_TEMP_AGE = 3600  # seconds


class PatcherError(Exception):
    """A bootstrap failure whose message is written for the user."""


def has_patcher(ctx: ServerContext) -> bool:
    """True when this server ships a patcher — what every UI surface hides behind."""
    return bool(ctx.patcher_url and ctx.patcher_exe)


def validate_patcher_exe(name: str) -> str:
    """Accept a bare filename only.

    Bundled entries are smoke-gated by the test suite; this is the runtime gate
    for custom servers, whose exe names are user-authored.
    """
    exe = str(name or "")
    if not exe:
        raise PatcherError("This server has no patcher file name configured.")
    try:
        # Windows rules regardless of host: the EQ folder is a Windows install.
        # Covers separators, drive prefixes, ADS colons, control chars, trailing
        # dots/spaces (Windows silently drops them), and CON/NUL-style names.
        validate_filename(exe, platform="windows")
    except ValidationError as exc:
        raise PatcherError(
            f"Invalid patcher file name {exe!r}: it must be a bare Windows file name."
        ) from exc
    if exe in (".", ".."):  # pathvalidate lets the dot dirs through
        raise PatcherError(f"Invalid patcher file name {exe!r}: it must be a bare file name.")
    return exe


def patcher_path(ctx: ServerContext) -> Path:
    """Where this server's patcher lives once installed."""
    if not str(ctx.eqpath).strip():
        raise PatcherError(f"{ctx.label} has no EverQuest folder set.")
    return Path(ctx.eqpath) / validate_patcher_exe(ctx.patcher_exe)


def is_installed(ctx: ServerContext) -> bool:
    """True when the patcher is already on disk."""
    try:
        return patcher_path(ctx).is_file()
    except PatcherError:
        return False  # an entry we can't resolve can't be installed either


async def install(ctx: ServerContext) -> Path:
    """Fetch and install this server's patcher, returning where it landed.

    Every failure raises PatcherError carrying a message meant for the user.
    """
    if not has_patcher(ctx):
        raise PatcherError(f"{ctx.label} doesn't have a patcher to download.")
    target = patcher_path(ctx)
    if target.is_file():
        return target
    eqpath = Path(ctx.eqpath)
    if not eqpath.is_dir():
        raise PatcherError(f"The {ctx.label} EverQuest folder isn't there: {ctx.eqpath}")

    if not _is_zip_url(ctx.patcher_url):
        # download_file_async stages to a temp file and swaps last, so a partial
        # download can't latch as installed either.
        await _fetch(ctx, target)
        if not target.is_file():
            raise PatcherError(f"The {ctx.label} patcher didn't download.")
        return target

    utils.sweep_stale_dirs(eqpath, TEMP_PREFIX, _STALE_TEMP_AGE)
    try:
        # Inside the EQ folder on purpose: %TEMP% is often another volume, and
        # the final move has to be a same-volume rename.
        work_dir = Path(tempfile.mkdtemp(dir=eqpath, prefix=TEMP_PREFIX))
    except OSError as exc:
        raise PatcherError(f"Couldn't write to the {ctx.label} EverQuest folder: {exc}") from exc

    archive = work_dir / "patcher.zip"
    try:
        await _fetch(ctx, archive)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    # The work dir belongs to the thread from here on. Cancelling this await
    # doesn't stop the thread, so cleaning up out here would race a live move.
    await asyncio.to_thread(_unpack_and_clean, ctx, archive, work_dir, target)
    return target


def _unpack_and_clean(ctx: ServerContext, archive: Path, work_dir: Path, target: Path) -> None:
    try:
        _unpack_into_place(ctx, archive, work_dir, target)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _is_zip_url(url: str) -> bool:
    """Decided by the entry data, never is_zipfile() — SFX installers are zipfiles too."""
    return urlsplit(url).path.lower().endswith(".zip")


async def _fetch(ctx: ServerContext, dest: Path) -> None:
    """Download the patcher URL to *dest* with no credentials attached."""
    try:
        async with net.new_unauth_client() as client:
            ok = await download.download_file_async(client, ctx.patcher_url, dest)
    except httpx.HTTPStatusError as exc:
        raise PatcherError(_http_status_message(ctx, exc)) from exc
    except httpx.HTTPError as exc:
        raise PatcherError(f"Couldn't reach the {ctx.label} website: {exc}") from exc
    except OSError as exc:
        raise PatcherError(f"Couldn't save the {ctx.label} patcher: {exc}") from exc
    if not ok:
        raise PatcherError(f"The download from the {ctx.label} website didn't finish.")


def _http_status_message(ctx: ServerContext, exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    if status != 403:
        return f"The {ctx.label} website returned an error ({status})."
    # Cloudflare bot mitigation gates on IP reputation: VPN and datacenter IPs
    # get walled while most residential users never see it. Their wall, not our bug.
    mitigated = exc.response.headers.get("cf-mitigated")
    if mitigated:
        print(f"{ctx.patcher_url} returned 403 (cf-mitigated: {mitigated})")
    message = (
        f"The {ctx.label} website is blocking your connection "
        "(their bot protection flags some IPs — VPNs especially)."
    )
    if ctx.guide:
        message += f" See their setup guide: {ctx.guide}"
    return message


def _unpack_into_place(ctx: ServerContext, archive: Path, work_dir: Path, target: Path) -> None:
    """Extract, check we got what was promised, then move it in."""
    payload = work_dir / "payload"
    _extract_patcher_zip(archive, payload)
    exe_name = target.name
    if not (payload / exe_name).is_file():
        # Verified before anything moves, so a wrong archive installs nothing at all.
        raise PatcherError(
            f"The {ctx.label} download didn't contain {exe_name}, so nothing was installed."
        )
    _move_payload_into_place(payload, Path(ctx.eqpath), exe_name)


def _extract_patcher_zip(zip_path: Path, dest: Path) -> None:
    """Extract a patcher archive into *dest*, a folder we own and can throw away.

    Deliberately not download.extract_and_discard_zip: that one stages swaps onto
    a live install, has two failure shapes and eats the source zip. Hostile member
    names (leading slashes, drives, "..") are neutralized by extractall itself,
    which sanitizes them to land inside *dest*; the promised-exe check afterward
    decides whether anything installs.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(zip_path) as archive:
            limit_error = download.check_zip_limits(archive.infolist())
            if limit_error:
                raise PatcherError(f"This patcher download looks wrong: {limit_error}")
            archive.extractall(dest)
    except (BadZipFile, zlib.error) as exc:  # bad CRC raises BadZipFile; a truncated deflate raises zlib.error
        raise PatcherError(f"The patcher download is corrupt: {exc}") from exc
    except OSError as exc:
        raise PatcherError(f"Couldn't unpack the patcher download: {exc}") from exc


def _move_payload_into_place(payload: Path, eqpath: Path, exe_name: str) -> None:
    """Move the extracted payload into the EQ folder, the patcher exe last."""
    # Exe last: it's the completion marker, so a move that dies partway still
    # reads as "not installed" and the next attempt starts clean.
    entries = sorted(payload.iterdir(), key=lambda p: os.path.normcase(p.name) == os.path.normcase(exe_name))
    for source in entries:
        destination = eqpath / source.name
        if destination.exists() and (destination.is_dir() or source.is_dir()):
            # Replacing a whole folder isn't ours to decide, and os.replace can't anyway.
            raise PatcherError(
                f"Can't install {source.name!r}: something with that name is already in {eqpath}."
            )
        try:
            source.replace(destination)
        except OSError as exc:
            raise PatcherError(f"Couldn't put {source.name!r} into {eqpath}: {exc}") from exc
