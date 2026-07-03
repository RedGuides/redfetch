"""Tests for two-phase staged extraction: stage every member, then swap all in."""

import os
import stat
import sys
import time
import zipfile

import pytest

from redfetch import download, utils


def test_is_safe_path_allows_children_and_blocks_traversal(tmp_path):
    assert utils.is_safe_path(str(tmp_path), str(tmp_path / "sub" / "file.lua")) is True
    assert utils.is_safe_path(str(tmp_path), str(tmp_path)) is True
    assert utils.is_safe_path(str(tmp_path), str(tmp_path / ".." / "evil.lua")) is False


@pytest.mark.skipif(sys.platform != "win32", reason="drive semantics")
def test_is_safe_path_cross_drive_is_false_not_an_error():
    # commonpath raised ValueError for paths on different drives; must just be False.
    assert utils.is_safe_path("C:\\base", "D:\\evil.dll") is False


def _make_stale(path):
    """Backdate a sidecar past the sweep's age threshold so it reads as prior-run debris."""
    old = time.time() - download._STALE_DEBRIS_AGE - 60
    os.utime(path, (old, old))


def _open_zip(tmp_path, files):
    zpath = tmp_path / "res.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return zipfile.ZipFile(zpath)


def test_extracts_and_swaps_all_members(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    with _open_zip(tmp_path, {"a.txt": b"new-a", "sub/b.txt": b"new-b"}) as zf:
        download.extract_with_structure(zf, str(dest), [])

    assert (dest / "a.txt").read_bytes() == b"new-a"
    assert (dest / "sub" / "b.txt").read_bytes() == b"new-b"
    assert not list(dest.rglob("*.rfnew"))


def test_stage_failure_aborts_before_any_swap(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"old-a")
    (dest / "b.txt").write_bytes(b"old-b")

    real_copy = download.shutil.copyfileobj
    calls = {"n": 0}

    def flaky_copy(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(download.shutil, "copyfileobj", flaky_copy)
    with _open_zip(tmp_path, {"a.txt": b"new-a", "b.txt": b"new-b"}) as zf:
        with pytest.raises(OSError):
            download.extract_with_structure(zf, str(dest), [])

    # Nothing swapped and no staged temps left: the install is never half-updated.
    assert (dest / "a.txt").read_bytes() == b"old-a"
    assert (dest / "b.txt").read_bytes() == b"old-b"
    assert not list(dest.rglob("*.rfnew"))


def test_swap_failure_still_applies_remaining_files(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"old-a")
    (dest / "b.txt").write_bytes(b"old-b")

    real_swap = download._swap_into_place

    def flaky_swap(tmp, target):
        if target.endswith("a.txt"):
            raise PermissionError("held open")
        return real_swap(tmp, target)

    monkeypatch.setattr(download, "_swap_into_place", flaky_swap)
    with _open_zip(tmp_path, {"a.txt": b"new-a", "b.txt": b"new-b"}) as zf:
        with pytest.raises(OSError):
            download.extract_with_structure(zf, str(dest), [])

    assert (dest / "a.txt").read_bytes() == b"old-a"   # held-open file keeps old bytes
    assert (dest / "b.txt").read_bytes() == b"new-b"   # everything swappable still applied


def test_skip_if_identical_leaves_file_untouched(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "a.txt"
    target.write_bytes(b"same")
    before = target.stat().st_mtime_ns

    with _open_zip(tmp_path, {"a.txt": b"same"}) as zf:
        download.extract_with_structure(zf, str(dest), [])

    assert target.stat().st_mtime_ns == before
    assert not list(dest.rglob("*.rfnew"))


def test_duplicate_flatten_targets_last_member_wins(tmp_path):
    # both members map to one target; pre-dedupe this crashed the second swap
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "config.lua").write_bytes(b"old")

    with _open_zip(tmp_path, {"x/config.lua": b"111", "y/config.lua": b"222"}) as zf:
        download.extract_flattened(zf, str(dest), [])

    assert (dest / "config.lua").read_bytes() == b"222"
    assert not list(dest.rglob("*.rfnew"))


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive filesystem")
def test_case_colliding_members_dedupe_to_one_swap(tmp_path):
    # distinct zip entries, same NTFS file: dedupe must be case-insensitive
    dest = tmp_path / "dest"
    dest.mkdir()
    with _open_zip(tmp_path, {"Same.txt": b"111", "same.txt": b"222"}) as zf:
        download.extract_with_structure(zf, str(dest), [])

    files = list(dest.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"222"


def test_nonpermission_swap_failure_isolated(tmp_path, monkeypatch):
    # AV-quarantine class: a vanished .rfnew must not abort the remaining swaps
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.txt").write_bytes(b"old-a")
    (dest / "b.txt").write_bytes(b"old-b")

    real_swap = download._swap_into_place

    def flaky_swap(tmp, target):
        if target.endswith("a.txt"):
            raise FileNotFoundError("rfnew vanished")
        return real_swap(tmp, target)

    monkeypatch.setattr(download, "_swap_into_place", flaky_swap)
    with _open_zip(tmp_path, {"a.txt": b"new-a", "b.txt": b"new-b"}) as zf:
        with pytest.raises(OSError):
            download.extract_with_structure(zf, str(dest), [])

    assert (dest / "a.txt").read_bytes() == b"old-a"
    assert (dest / "b.txt").read_bytes() == b"new-b"
    assert not list(dest.rglob("*.rfnew"))


def test_readonly_target_updated_in_place(tmp_path, monkeypatch):
    # CD-era installs: read-only maps must update without displacement debris.
    # simulate Windows semantics (replace onto RO dst fails) so posix CI pins this too
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "map.txt"
    target.write_bytes(b"old")
    os.chmod(target, stat.S_IREAD)

    real_replace = os.replace

    def readonly_replace(src, dst):
        if os.path.exists(dst) and not os.access(dst, os.W_OK):
            raise PermissionError("read-only destination")
        return real_replace(src, dst)

    monkeypatch.setattr(download.os, "replace", readonly_replace)
    with _open_zip(tmp_path, {"map.txt": b"new"}) as zf:
        download.extract_with_structure(zf, str(dest), [])

    assert target.read_bytes() == b"new"
    assert not list(dest.rglob("*.rfold*"))


def test_remove_if_exists_clears_readonly(tmp_path, monkeypatch):
    # displaced .rfold files keep the read-only bit; sweep must still delete them.
    # simulate Windows semantics (unlink of RO file fails) so posix CI pins this too
    debris = tmp_path / "old.dll.rfold"
    debris.write_bytes(b"x")
    os.chmod(debris, stat.S_IREAD)

    real_remove = os.remove

    def readonly_remove(path):
        if not os.access(path, os.W_OK):
            raise PermissionError("read-only file")
        real_remove(path)

    monkeypatch.setattr(download.os, "remove", readonly_remove)
    download._remove_if_exists(str(debris))

    assert not debris.exists()


def test_directory_shadowing_a_file_member_fails_loudly(tmp_path):
    # never silently displace a directory (and the user files inside) to .rfold
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "data").mkdir()
    (dest / "data" / "keep.txt").write_bytes(b"user file")

    with _open_zip(tmp_path, {"data": b"now a file"}) as zf:
        with pytest.raises(OSError):
            download.extract_with_structure(zf, str(dest), [])

    assert (dest / "data" / "keep.txt").read_bytes() == b"user file"
    assert not list(dest.rglob("*.rfold*"))
    assert not list(dest.rglob("*.rfnew"))


def test_displaced_rfold_survives_same_run_sweep(tmp_path, monkeypatch):
    # rename keeps the old file's mtime: without a fresh stamp, a concurrent
    # extraction's sweep would eat the .rfold before a rollback could use it
    target = tmp_path / "MQ2Foo.dll"
    target.write_bytes(b"old")
    _make_stale(target)
    tmp = tmp_path / "MQ2Foo.dll.rfnew"
    tmp.write_bytes(b"new")

    real_replace = os.replace
    state = {"raised": False}

    def fake_replace(src, dst):
        if not state["raised"] and os.fspath(src) == str(tmp):
            state["raised"] = True
            raise PermissionError("mapped image")
        return real_replace(src, dst)

    monkeypatch.setattr(download.os, "replace", fake_replace)

    assert download._swap_into_place(str(tmp), str(target)) is True
    download.sweep_stale_swap_files(str(tmp_path))

    assert (tmp_path / "MQ2Foo.dll.rfold").read_bytes() == b"old"


def test_locked_swap_falls_back_to_numbered_rfold(tmp_path, monkeypatch):
    # multibox: the canonical .rfold is still a mapped image from a prior update
    target = tmp_path / "MQ2Foo.dll"
    target.write_bytes(b"gen2")
    stale = tmp_path / "MQ2Foo.dll.rfold"
    stale.write_bytes(b"gen1")
    tmp = tmp_path / "MQ2Foo.dll.rfnew"
    tmp.write_bytes(b"gen3")

    real_replace = os.replace
    state = {"fastpath_raised": False}

    def fake_replace(src, dst):
        if not state["fastpath_raised"] and os.fspath(src) == str(tmp) and os.fspath(dst) == str(target):
            state["fastpath_raised"] = True
            raise PermissionError("target is a mapped image")
        return real_replace(src, dst)

    monkeypatch.setattr(download.os, "replace", fake_replace)
    monkeypatch.setattr(download, "_remove_if_exists", lambda path: None)  # sidecars "locked"

    assert download._swap_into_place(str(tmp), str(target)) is True
    assert target.read_bytes() == b"gen3"
    assert stale.read_bytes() == b"gen1"  # the mapped prior generation was left alone
    assert (tmp_path / "MQ2Foo.dll.rfold1").read_bytes() == b"gen2"


def test_failed_final_swap_rolls_back_displaced_target(tmp_path, monkeypatch):
    # AV holds the .rfnew: the displaced original must come back, never a missing target
    target = tmp_path / "MQ2Foo.dll"
    target.write_bytes(b"old")
    tmp = tmp_path / "MQ2Foo.dll.rfnew"
    tmp.write_bytes(b"new")

    real_replace = os.replace

    def fake_replace(src, dst):
        if os.fspath(src) == str(tmp) and os.fspath(dst) == str(target):
            raise PermissionError("rfnew held by scanner")
        return real_replace(src, dst)

    monkeypatch.setattr(download.os, "replace", fake_replace)

    with pytest.raises(PermissionError):
        download._swap_into_place(str(tmp), str(target))

    assert target.read_bytes() == b"old"
    assert not (tmp_path / "MQ2Foo.dll.rfold").exists()
    assert not tmp.exists()


def test_sweep_recurses_and_removes_sidecars(tmp_path):
    (tmp_path / "plugins").mkdir()
    keep = tmp_path / "keep.dll"
    keep.write_bytes(b"x")
    for stale in (tmp_path / "old.dll.rfold", tmp_path / "gen1.dll.rfold1",
                  tmp_path / "plugins" / "new.dll.rfnew"):
        stale.write_bytes(b"x")
        _make_stale(stale)

    download.sweep_stale_swap_files(str(tmp_path))

    assert keep.exists()
    assert not (tmp_path / "old.dll.rfold").exists()
    assert not (tmp_path / "gen1.dll.rfold1").exists()
    assert not (tmp_path / "plugins" / "new.dll.rfnew").exists()


def test_sweep_spares_fresh_sidecars(tmp_path):
    # A fresh .rfnew is another concurrent extraction's in-flight staging, not debris.
    fresh = tmp_path / "a.lua.rfnew"
    fresh.write_bytes(b"x")

    download.sweep_stale_swap_files(str(tmp_path))

    assert fresh.exists()


def test_startup_debris_sweep_covers_install_dirs(tmp_path, monkeypatch):
    vvmq = tmp_path / "vvmq"
    vvmq.mkdir()
    debris = vvmq / "MacroQuest.exe.rfold"
    debris.write_bytes(b"x")
    _make_stale(debris)
    monkeypatch.setattr(utils, "get_vvmq_path", lambda: str(vvmq))
    monkeypatch.setattr(utils, "get_myseq_path", lambda: None)
    # A missing/unconfigured dir must not break the sweep of the others.
    monkeypatch.setattr(utils, "get_current_download_folder", lambda: str(tmp_path / "missing"))

    utils.sweep_stale_update_debris()

    assert not debris.exists()
