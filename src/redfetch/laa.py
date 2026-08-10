"""Read and set an exe's Large Address Aware flag, the one that lifts its 2GB cap."""
# standard
import os
import shutil
import struct
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

# third-party
import pefile

# local
from redfetch import detecteq
from redfetch import processes


FILENAME = "eqgame.exe"
BACKUP_NAME = "eqgame.exe.bak"

# set the "Characteristics" member IMAGE_FILE_LARGE_ADDRESS_AWARE
# https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-image_file_header
_LAA_FLAG = 0x0020


class LaaError(Exception):
    """A refusal whose message is written for the user."""


class LaaState(Enum):
    HIDDEN = auto()     # no folder chosen yet
    BLOCKED = auto()    # no eqgame.exe, or one that isn't readable
    ON = auto()
    OFF = auto()


@dataclass(frozen=True, slots=True)
class LaaStatus:
    """What the 4GB button says."""
    state: LaaState
    problem: str = ""


def read_flag(exe_path: str) -> bool:
    """The Large Address Aware flag from the COFF header."""
    try:
        with open(exe_path, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                raise LaaError(f"{FILENAME} doesn't look like a Windows program.")
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            f.seek(e_lfanew)
            # Characteristics is its last field.
            coff = f.read(24)
            if len(coff) < 24 or coff[:4] != b"PE\x00\x00":
                raise LaaError(f"{FILENAME} doesn't look like a Windows program.")
            characteristics = struct.unpack_from("<H", coff, 22)[0]
    except OSError as exc:
        raise LaaError(f"Couldn't read {FILENAME}: {exc}") from exc
    return bool(characteristics & _LAA_FLAG)


def status(eqpath: str) -> LaaStatus:
    """What the folder's eqgame.exe says about its memory limit."""
    if not str(eqpath).strip():
        return LaaStatus(LaaState.HIDDEN)
    if not detecteq.is_valid_eq_dir(eqpath):
        return LaaStatus(
            LaaState.BLOCKED, problem=f"No {FILENAME} in {eqpath}, so there's no flag to set."
        )
    try:
        flagged = read_flag(os.path.join(eqpath, FILENAME))
    except LaaError as exc:
        return LaaStatus(LaaState.BLOCKED, problem=str(exc))
    return LaaStatus(LaaState.ON if flagged else LaaState.OFF)


def enable(eqpath: str) -> None:
    """Set the flag on the folder's eqgame.exe, keeping the original as a backup."""
    info = status(eqpath)
    if info.state is LaaState.HIDDEN:
        raise LaaError("Pick this server's EverQuest folder first.")
    if info.state is LaaState.BLOCKED:
        raise LaaError(info.problem)
    if info.state is LaaState.ON:
        return

    running = processes.are_executables_running_in_folder(eqpath)
    if running:
        names = ", ".join(sorted({os.path.basename(path) for _, path in running}))
        raise LaaError(f"{names} is running from that folder — close it first, then try again.")

    exe_path = os.path.join(eqpath, FILENAME)
    try:
        data = Path(exe_path).read_bytes()
    except OSError as exc:
        raise LaaError(f"Couldn't read {FILENAME}: {exc}") from exc

    try:
        shutil.copy2(exe_path, os.path.join(eqpath, BACKUP_NAME))
    except OSError as exc:
        raise LaaError(f"Couldn't save a backup ({BACKUP_NAME}): {exc}") from exc

    tmp_path = exe_path + ".tmp"
    try:
        # data=, not the path: pefile must not hold a handle on the exe we replace.
        pe = pefile.PE(data=data, fast_load=True)
        pe.FILE_HEADER.Characteristics |= _LAA_FLAG
        # recalc so we don't spook AV.
        pe.OPTIONAL_HEADER.CheckSum = pe.generate_checksum()
        pe.write(filename=tmp_path)
        pe.close()
        os.replace(tmp_path, exe_path)
    except pefile.PEFormatError as exc:
        raise LaaError(f"{FILENAME} isn't a program Windows recognizes: {exc}") from exc
    except OSError as exc:
        raise LaaError(f"Couldn't update {FILENAME}: {exc}") from exc
    finally:
        with suppress(OSError):
            os.remove(tmp_path)

    # Independent of pefile: the same stdlib read the button trusts.
    if not read_flag(exe_path):
        raise LaaError(f"The flag didn't stick — {FILENAME} still reads as 2GB-limited.")
