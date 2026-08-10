"""eqgame.exe's 4GB flag: what the reader finds, what enable refuses, and the one rewrite."""
import os
import struct
from types import SimpleNamespace

import pefile
import pytest

from redfetch import laa


def _pe_bytes(*, laa_on=False):
    """A minimal 32-bit PE that both the stdlib reader and pefile can parse."""
    chars = 0x0102 | (0x0020 if laa_on else 0)
    e_lfanew = 0x80
    dos = (b"MZ" + b"\x00" * 58 + struct.pack("<I", e_lfanew)).ljust(e_lfanew, b"\x00")
    coff = b"PE\x00\x00" + struct.pack("<HHIIIHH", 0x014C, 0, 0, 0, 0, 224, chars)
    opt = struct.pack(
        "<HBB6I3I6H4I2H4I2I",
        0x010B, 0, 0,           # PE32 magic, linker versions
        0, 0, 0, 0, 0x1000, 0x1000,
        0x400000, 0x1000, 0x200,
        4, 0, 0, 0, 5, 0,
        0, 0x1000, 0x200, 0,    # win32ver, SizeOfImage, SizeOfHeaders, CheckSum
        2, 0,                   # GUI subsystem
        0x100000, 0x1000, 0x100000, 0x1000,
        0, 16,
    ) + b"\x00" * 128           # 16 empty data directories
    return (dos + coff + opt).ljust(0x200, b"\x00")


def _eq(tmp_path, body=None):
    """A folder whose eqgame.exe is a real (synthetic) PE, flag off by default."""
    folder = tmp_path / "EQ"
    folder.mkdir()
    (folder / laa.FILENAME).write_bytes(_pe_bytes() if body is None else body)
    return folder


# --- the reader ---


@pytest.mark.parametrize("laa_on", [False, True])
def test_reader_reads_the_flag_both_ways(tmp_path, laa_on):
    exe = tmp_path / laa.FILENAME
    exe.write_bytes(_pe_bytes(laa_on=laa_on))
    assert laa.read_flag(str(exe)) is laa_on


@pytest.mark.parametrize(
    "body",
    [
        b"",                                   # empty
        b"MZ",                                 # the two-byte stub other tests use
        b"MZ" + b"\x00" * 200,                 # e_lfanew of 0 lands on "MZ", not "PE\0\0"
        b"hello, I am not a program" * 20,     # no MZ at all
        _pe_bytes()[:0x84],                    # truncated mid PE signature
    ],
)
def test_reader_refuses_a_file_that_isnt_a_program(tmp_path, body):
    exe = tmp_path / laa.FILENAME
    exe.write_bytes(body)
    with pytest.raises(laa.LaaError, match="Windows program"):
        laa.read_flag(str(exe))


def test_reader_checks_mz_not_just_the_pe_signature(tmp_path):
    """A non-MZ file whose 0x3C points at a valid PE block must still refuse."""
    exe = tmp_path / laa.FILENAME
    exe.write_bytes(b"XX" + _pe_bytes()[2:])
    with pytest.raises(laa.LaaError, match="Windows program"):
        laa.read_flag(str(exe))


def test_reader_reports_a_missing_file(tmp_path):
    with pytest.raises(laa.LaaError, match="Couldn't read"):
        laa.read_flag(str(tmp_path / laa.FILENAME))


def test_reader_survives_an_e_lfanew_past_the_end(tmp_path):
    body = bytearray(_pe_bytes())
    body[0x3C:0x40] = struct.pack("<I", 0x7FFFFFFF)
    exe = tmp_path / laa.FILENAME
    exe.write_bytes(bytes(body))
    with pytest.raises(laa.LaaError, match="Windows program"):
        laa.read_flag(str(exe))


# --- status ---


@pytest.mark.parametrize("eqpath", ["", "   "])
def test_status_hides_without_a_folder(eqpath):
    assert laa.status(eqpath).state is laa.LaaState.HIDDEN


def test_status_blocks_on_a_folder_that_isnt_everquest(tmp_path):
    plain = tmp_path / "not-eq"
    plain.mkdir()
    info = laa.status(str(plain))
    assert info.state is laa.LaaState.BLOCKED
    assert laa.FILENAME in info.problem
    gone = tmp_path / "gone"
    assert laa.status(str(gone)).state is laa.LaaState.BLOCKED


def test_status_blocks_on_an_exe_that_isnt_a_program(tmp_path):
    folder = _eq(tmp_path, body=b"MZ")
    info = laa.status(str(folder))
    assert info.state is laa.LaaState.BLOCKED
    assert "Windows program" in info.problem


@pytest.mark.parametrize(
    "laa_on, state", [(False, laa.LaaState.OFF), (True, laa.LaaState.ON)]
)
def test_status_reads_the_flag(tmp_path, laa_on, state):
    folder = _eq(tmp_path, body=_pe_bytes(laa_on=laa_on))
    assert laa.status(str(folder)).state is state


# --- enable ---


def test_enable_sets_the_flag_and_keeps_the_original(tmp_path):
    folder = _eq(tmp_path)
    original = (folder / laa.FILENAME).read_bytes()
    laa.enable(str(folder))
    assert laa.read_flag(str(folder / laa.FILENAME)) is True
    assert (folder / laa.BACKUP_NAME).read_bytes() == original
    pe = pefile.PE(data=(folder / laa.FILENAME).read_bytes())
    stored, regenerated = pe.OPTIONAL_HEADER.CheckSum, pe.generate_checksum()
    pe.close()
    assert stored == regenerated and stored != 0
    assert sorted(p.name for p in folder.iterdir()) == [laa.FILENAME, laa.BACKUP_NAME]


def test_enable_is_a_noop_when_the_flag_is_already_on(tmp_path):
    folder = _eq(tmp_path, body=_pe_bytes(laa_on=True))
    baseline = (folder / laa.FILENAME).read_bytes()
    laa.enable(str(folder))
    assert (folder / laa.FILENAME).read_bytes() == baseline
    assert not (folder / laa.BACKUP_NAME).exists()


def test_enable_refreshes_a_stale_backup(tmp_path):
    """The backup is always the exe as it was before the last change."""
    folder = _eq(tmp_path)
    original = (folder / laa.FILENAME).read_bytes()
    (folder / laa.BACKUP_NAME).write_bytes(b"an older backup")
    laa.enable(str(folder))
    assert (folder / laa.BACKUP_NAME).read_bytes() == original


def test_enable_refuses_without_a_folder_at_all():
    with pytest.raises(laa.LaaError, match="folder first"):
        laa.enable("")


def test_enable_refuses_without_a_real_everquest_folder(tmp_path):
    # "no flag to set" pins the deliberate refusal, not an incidental read error.
    gone = tmp_path / "gone"
    with pytest.raises(laa.LaaError, match="no flag to set"):
        laa.enable(str(gone))
    assert not gone.exists()
    plain = tmp_path / "not-eq"
    plain.mkdir()
    with pytest.raises(laa.LaaError, match="no flag to set"):
        laa.enable(str(plain))
    assert os.listdir(plain) == []


def test_enable_refuses_while_something_runs_from_the_folder(tmp_path, monkeypatch):
    folder = _eq(tmp_path)
    baseline = (folder / laa.FILENAME).read_bytes()
    monkeypatch.setattr(
        laa.processes, "are_executables_running_in_folder",
        lambda path: [(4242, str(folder / laa.FILENAME))],
    )
    with pytest.raises(laa.LaaError, match="close it first"):
        laa.enable(str(folder))
    assert (folder / laa.FILENAME).read_bytes() == baseline
    assert not (folder / laa.BACKUP_NAME).exists()


def test_enable_refuses_when_the_backup_fails(tmp_path, monkeypatch):
    """No backup, no write — the archival copy is part of the contract."""
    folder = _eq(tmp_path)
    baseline = (folder / laa.FILENAME).read_bytes()
    monkeypatch.setattr(
        laa.shutil, "copy2",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    with pytest.raises(laa.LaaError, match="backup"):
        laa.enable(str(folder))
    assert (folder / laa.FILENAME).read_bytes() == baseline


def test_enable_cleans_up_when_the_swap_fails(tmp_path, monkeypatch):
    folder = _eq(tmp_path)
    baseline = (folder / laa.FILENAME).read_bytes()
    monkeypatch.setattr(
        laa.os, "replace",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "locked")),
    )
    with pytest.raises(laa.LaaError, match="Couldn't update"):
        laa.enable(str(folder))
    assert (folder / laa.FILENAME).read_bytes() == baseline
    # The backup came first (fine to keep), and the temp file was swept.
    assert sorted(p.name for p in folder.iterdir()) == [laa.FILENAME, laa.BACKUP_NAME]


def test_enable_wraps_a_pefile_refusal(tmp_path, monkeypatch):
    folder = _eq(tmp_path)
    baseline = (folder / laa.FILENAME).read_bytes()
    monkeypatch.setattr(
        pefile, "PE",
        lambda *a, **k: (_ for _ in ()).throw(pefile.PEFormatError("mangled")),
    )
    with pytest.raises(laa.LaaError, match="Windows recognizes"):
        laa.enable(str(folder))
    assert (folder / laa.FILENAME).read_bytes() == baseline
    assert sorted(p.name for p in folder.iterdir()) == [laa.FILENAME, laa.BACKUP_NAME]


def test_enable_notices_when_the_flag_doesnt_stick(tmp_path, monkeypatch):
    """The final check is the stdlib reader, independent of whatever pefile wrote."""

    class InertPE:
        """Writes its input back unmodified, flag never set."""

        def __init__(self, data=None, fast_load=False):
            self._data = data
            self.FILE_HEADER = SimpleNamespace(Characteristics=0)
            self.OPTIONAL_HEADER = SimpleNamespace(CheckSum=0)

        def generate_checksum(self):
            return 0

        def write(self, filename):
            with open(filename, "wb") as f:
                f.write(self._data)

        def close(self):
            pass

    folder = _eq(tmp_path)
    monkeypatch.setattr(pefile, "PE", InertPE)
    with pytest.raises(laa.LaaError, match="didn't stick"):
        laa.enable(str(folder))
