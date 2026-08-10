"""Provisioning a server folder from a clean RoF2 source: discovery, the copy, the chain."""
import asyncio
import dataclasses
import io
import os
import stat
import zipfile
from collections import namedtuple
from pathlib import Path

import pycdlib
import pytest
import typer
from rich.filesize import decimal

from conftest import _install_settings
from redfetch import config, detecteq, laa, main, patcher, provision, servers, utils


shutil_usage = namedtuple("usage", "total used free")

EQGAME = b"MZ" + b"eqgame bytes " * 8
FIXTURE = {
    "clientfolder/eqgame.exe": EQGAME,
    "clientfolder/eqclient.ini": b"[Defaults]\n",
    "clientfolder/uifiles/default/window.xml": b"<XML/>",
}
# FIXTURE as the game folder itself — the only shape a tree source may take.
GAME_FILES = {member.removeprefix("clientfolder/"): data for member, data in FIXTURE.items()}


# --- fixtures ----------------------------------------------------------------

def _zip(tmp_path, members=None, name="fixture.zip", dirs=()):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for entry in dirs:
            archive.writestr(zipfile.ZipInfo(entry), b"")
        for member, data in (FIXTURE if members is None else members).items():
            archive.writestr(member, data)
    return path


def _iso(tmp_path, members=None, name="fixture.iso", *, flavor="joliet", dirs=()):
    """A tiny disc authored by pycdlib itself, in whichever namespace the test is about.

    A trailing slash in *dirs* makes an empty directory.
    """
    path = tmp_path / name
    image = pycdlib.PyCdlib()
    image.new(**{
        "joliet": {"joliet": 3},
        "udf": {"udf": "2.60"},
        "rr": {"interchange_level": 4, "rock_ridge": "1.09"},
        "plain": {"interchange_level": 4},  # no richer namespace: raw 8.3-ish names
    }[flavor])

    def raw(parts):
        return "/" + "/".join(part.upper() for part in parts)

    def add(parts, data=None):
        where = "/" + "/".join(parts)
        if flavor in ("joliet", "udf"):
            key = {"joliet": "joliet_path", "udf": "udf_path"}[flavor]
            if data is None:
                image.add_directory(**{key: where})
            else:
                image.add_fp(io.BytesIO(data), len(data), **{key: where})
        else:
            extra = {"rr_name": parts[-1]} if flavor == "rr" else {}
            if data is None:
                image.add_directory(raw(parts), **extra)
            else:
                image.add_fp(io.BytesIO(data), len(data), raw(parts) + ";1", **extra)

    members = FIXTURE if members is None else members
    made = set()
    for member in list(members) + list(dirs):
        parts = member.strip("/").split("/")
        depth = len(parts) if member.endswith("/") else len(parts) - 1
        for i in range(1, depth + 1):
            key = tuple(p.lower() for p in parts[:i])
            if key not in made:
                made.add(key)
                add(parts[:i])
    for member, data in members.items():
        add(member.strip("/").split("/"), data)
    image.write(str(path))
    image.close()
    return path


def _tree(tmp_path, members=None, name="source"):
    root = tmp_path / name
    for member, data in (FIXTURE if members is None else members).items():
        target = root / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _plan(source, kind=None):
    kind = kind or provision.classify_source(str(source))
    return provision.scan_source(Path(source), kind)


def _materialize(source, destination, **kwargs):
    source = Path(source)
    provision.materialize(source, _plan(source), Path(destination), **kwargs)


def _provision(tmp_path, destination=None, source=None):
    return asyncio.run(provision.provision(
        "lazarus", env="EMU",
        source=str(_zip(tmp_path)) if source is None else source,
        destination=str(destination or tmp_path / "new"),
    ))


def _temp_dirs(parent):
    return [n for n in os.listdir(parent) if n.startswith(provision.TEMP_PREFIX)]


def _usage(*, free):
    return shutil_usage(total=0, used=0, free=free)


@pytest.fixture
def served_lazarus(tmp_path, monkeypatch):
    """A configured lazarus whose EverQuest folder is on disk."""
    served = tmp_path / "eq_lazarus"
    served.mkdir()
    _install_settings(tmp_path, monkeypatch, local_toml=f"""
[EMU.SERVERS.lazarus]
opt_in = true
eqpath = "{served.as_posix()}"
""")
    return served


# --- shape dispatch ----------------------------------------------------------

def test_a_directory_is_a_tree_source(tmp_path):
    assert provision.classify_source(str(_tree(tmp_path))) is provision.SourceKind.TREE


@pytest.mark.parametrize("name", ["fixture.zip", "FIXTURE.ZIP"])
def test_zip_extension_is_a_zip_source(tmp_path, name):
    assert provision.classify_source(str(_zip(tmp_path, name=name))) is provision.SourceKind.ZIP


@pytest.mark.parametrize("name", ["fixture.iso", "FIXTURE.ISO"])
def test_iso_extension_is_an_iso_source(tmp_path, name):
    assert provision.classify_source(str(_iso(tmp_path, name=name))) is provision.SourceKind.ISO


def test_other_extensions_name_the_shapes(tmp_path):
    other = tmp_path / "rof2.rar"
    other.write_bytes(b"nope")
    with pytest.raises(provision.ProvisionError, match=r"\.zip, an \.iso, or a folder"):
        provision.classify_source(str(other))


def test_missing_source_says_so(tmp_path):
    with pytest.raises(provision.ProvisionError, match="isn't there anymore"):
        provision.classify_source(str(tmp_path / "gone.zip"))


def test_no_source_asks_for_one_without_saying_where(tmp_path):
    with pytest.raises(provision.ProvisionError) as caught:
        provision.classify_source("")
    assert str(caught.value) == provision.NO_SOURCE_MESSAGE


# --- EQ-root discovery -------------------------------------------------------

def test_zip_discovery_finds_the_nested_root(tmp_path):
    plan = _plan(_zip(tmp_path))
    assert plan.root == ("clientfolder",)
    assert plan.file_count == len(FIXTURE)
    assert plan.total_bytes == sum(len(data) for data in FIXTURE.values())


def test_zip_discovery_handles_a_root_level_eqgame(tmp_path):
    plan = _plan(_zip(tmp_path, {"eqgame.exe": EQGAME, "eqclient.ini": b"x"}))
    assert plan.root == ()
    assert plan.file_count == 2


def test_zip_discovery_ignores_members_outside_the_root(tmp_path):
    source = _zip(tmp_path, {**FIXTURE, "readme.txt": b"packaged by me", "notes/todo.md": b"x"})
    plan = _plan(source)
    assert plan.root == ("clientfolder",)
    assert plan.file_count == len(FIXTURE)  # the strays aren't copied, so they aren't measured


@pytest.mark.parametrize("make", [_zip, _iso, _tree], ids=["zip", "iso", "tree"])
def test_discovery_refuses_when_there_is_no_eqgame(tmp_path, make):
    source = make(tmp_path, {"clientfolder/eqclient.ini": b"x"})
    with pytest.raises(provision.ProvisionError, match="isn't a RoF2 copy"):
        _plan(source)


@pytest.mark.parametrize("make", [_zip, _iso], ids=["zip", "iso"])
def test_discovery_refuses_two_roots_and_lists_them(tmp_path, make):
    """Archives only: a tree source isn't searched, so it can't have two roots."""
    source = make(tmp_path, {"a/eqgame.exe": EQGAME, "b/eqgame.exe": EQGAME})
    with pytest.raises(provision.ProvisionError) as caught:
        _plan(source)
    message = str(caught.value)
    assert "more than one EverQuest folder" in message
    listed = message.splitlines()[-2:]
    assert sorted(line.strip() for line in listed) == ["a", "b"]


def test_zip_discovery_reads_backslash_members(tmp_path):
    """ZipFile doesn't normalize separators on read; discovery and the copy must agree."""
    source = _zip(tmp_path, {
        "clientfolder\\eqgame.exe": EQGAME,
        "clientfolder\\uifiles\\window.xml": b"<XML/>",
    })
    plan = _plan(source)
    assert plan.root == ("clientfolder",)
    assert plan.file_count == 2


def test_an_encrypted_zip_is_refused_at_scan_time(tmp_path):
    """Before anything is written — zipfile would otherwise raise a raw RuntimeError mid-copy."""
    source = _zip(tmp_path)
    raw = bytearray(source.read_bytes())
    pos = raw.find(b"PK\x01\x02")  # first central-directory record
    raw[pos + 8] |= 0x1  # the encryption flag
    source.write_bytes(bytes(raw))
    with pytest.raises(provision.ProvisionError, match="password"):
        _plan(source)


def test_an_unsupported_compression_method_is_refused_at_scan_time(tmp_path):
    """7-Zip offers Deflate64 one dropdown away, and zipfile can't read it."""
    source = _zip(tmp_path)
    raw = bytearray(source.read_bytes())
    pos = raw.find(b"PK\x01\x02")
    raw[pos + 10:pos + 12] = (9).to_bytes(2, "little")  # Deflate64
    source.write_bytes(bytes(raw))
    with pytest.raises(provision.ProvisionError, match="deflate64"):
        _plan(source)


def test_tree_discovery_takes_the_folder_itself(tmp_path):
    source = _tree(tmp_path, GAME_FILES)
    plan = _plan(source)
    assert plan.root == source
    assert plan.file_count == len(GAME_FILES)
    assert plan.total_bytes == sum(len(data) for data in GAME_FILES.values())


def test_tree_discovery_never_searches_subfolders(tmp_path):
    """Pointing at something broad like C:\\Users must refuse instantly, not walk it."""
    source = _tree(tmp_path)  # eqgame.exe sits one level down, in clientfolder/
    with pytest.raises(provision.ProvisionError, match="folder that holds"):
        _plan(source)


def test_iso_discovery_finds_the_nested_root(tmp_path):
    plan = _plan(_iso(tmp_path))
    assert plan.root == ("clientfolder",)
    assert plan.file_count == len(FIXTURE)
    assert plan.total_bytes == sum(len(data) for data in FIXTURE.values())


def test_iso_discovery_ignores_files_outside_the_root(tmp_path):
    """The real depot disc carries the tool it was downloaded with, beside the game."""
    source = _iso(tmp_path, {**FIXTURE, "downloader/readme.txt": b"packaged by me"})
    plan = _plan(source)
    assert plan.root == ("clientfolder",)
    assert plan.file_count == len(FIXTURE)


@pytest.mark.parametrize("raw, landed", [
    ("eqgame.exe", "eqgame.exe"),
    ("EQGAME.EXE;1", "EQGAME.EXE"),
    ("README.;1", "README"),  # an extensionless file, which a raw 9660 disc gives a bare dot
])
def test_on_disc_names_lose_their_version_and_empty_extension(raw, landed):
    assert provision._iso_name(raw) == landed


# --- destination and role guards ---------------------------------------------

def test_absent_and_empty_destinations_pass(tmp_path):
    source = _zip(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    provision.validate_destination(tmp_path / "new", source)
    provision.validate_destination(empty, source)


def test_non_empty_destination_is_refused_with_the_redirect(tmp_path):
    source = _zip(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "eqgame.exe").write_bytes(EQGAME)
    with pytest.raises(provision.ProvisionError) as caught:
        provision.validate_destination(occupied, source)
    assert "use an existing folder" in str(caught.value)


def test_destination_that_is_a_file_is_refused(tmp_path):
    source = _zip(tmp_path)
    target = tmp_path / "afile"
    target.write_bytes(b"x")
    with pytest.raises(provision.ProvisionError, match="is a file"):
        provision.validate_destination(target, source)


@pytest.mark.parametrize("relation", ["same", "inside_source", "contains_source"])
def test_destination_may_not_overlap_the_source(tmp_path, relation):
    source = _tree(tmp_path, name="clean")
    destination = {
        "same": source,
        "inside_source": source / "copy",
        "contains_source": tmp_path,
    }[relation]
    with pytest.raises(provision.ProvisionError, match="clean RoF2 copy"):
        provision.validate_destination(destination, source)


def test_clean_source_conflicts_names_a_configured_server(tmp_path, served_lazarus):
    assert provision.clean_source_conflicts(str(served_lazarus))
    assert "Project Lazarus" in provision.clean_source_conflicts(str(served_lazarus))[0]
    assert provision.clean_source_conflicts(str(tmp_path / "elsewhere")) == []


def test_set_clean_source_refuses_a_served_folder(served_lazarus, monkeypatch):
    written = []
    monkeypatch.setattr(config, "update_setting", lambda *args, **kw: written.append(args))
    with pytest.raises(provision.ProvisionError, match="isn't a clean copy"):
        provision.set_clean_source(str(served_lazarus))
    assert written == []


def test_the_clean_source_lands_in_emu_whatever_env_is_active(tmp_path, monkeypatch):
    """Reads are unconditionally from [EMU]; a write to the active env would be lost."""
    monkeypatch.setenv("REDFETCH_ENV", "LIVE")
    _install_settings(tmp_path, monkeypatch)
    source = str(tmp_path / "rof2.zip")
    provision.set_clean_source(source)
    assert provision.clean_source() == source


# --- free space --------------------------------------------------------------

def test_free_space_refusal_carries_both_numbers(tmp_path, monkeypatch):
    plan = _plan(_zip(tmp_path))
    monkeypatch.setattr(provision.shutil, "disk_usage", lambda _path: _usage(free=100))
    with pytest.raises(provision.ProvisionError) as caught:
        provision.check_free_space(plan, tmp_path / "new")
    message = str(caught.value)
    assert decimal(plan.total_bytes + provision.FREE_SPACE_CUSHION) in message
    assert decimal(100) in message


def test_free_space_probes_a_folder_that_exists(tmp_path, monkeypatch):
    """A brand-new destination has no parent yet — disk_usage raises on one of those."""
    plan = _plan(_zip(tmp_path))
    probed = []

    def usage(path):
        probed.append(Path(path))
        return _usage(free=plan.total_bytes + provision.FREE_SPACE_CUSHION)

    monkeypatch.setattr(provision.shutil, "disk_usage", usage)
    provision.check_free_space(plan, tmp_path / "not" / "there" / "yet")
    assert probed and probed[0].exists()


# --- materializing -----------------------------------------------------------

def test_zip_route_strips_the_prefix(tmp_path):
    destination = tmp_path / "new"
    _materialize(_zip(tmp_path), destination)
    assert (destination / "eqgame.exe").read_bytes() == EQGAME
    assert (destination / "uifiles/default/window.xml").read_bytes() == b"<XML/>"
    assert not (destination / "clientfolder").exists()
    assert _temp_dirs(tmp_path) == []


def test_zip_route_keeps_empty_directories(tmp_path):
    source = _zip(tmp_path, dirs=("clientfolder/", "clientfolder/logs/"))
    destination = tmp_path / "new"
    _materialize(source, destination)
    assert (destination / "logs").is_dir()


def test_zip_route_lands_backslash_members(tmp_path):
    source = _zip(tmp_path, {
        "clientfolder\\eqgame.exe": EQGAME,
        "clientfolder\\uifiles\\window.xml": b"<XML/>",
    })
    destination = tmp_path / "new"
    _materialize(source, destination)
    assert (destination / "uifiles/window.xml").read_bytes() == b"<XML/>"


def test_zip_route_refuses_a_traversal_member(tmp_path):
    source = _zip(tmp_path, {
        "clientfolder/eqgame.exe": EQGAME,
        "clientfolder/../escaped.txt": b"outside",
    })
    destination = tmp_path / "new"
    with pytest.raises(provision.ProvisionError, match="outside the new folder"):
        _materialize(source, destination)
    assert not destination.exists()
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize("make", [_zip, _iso], ids=["zip", "iso"])
def test_the_copy_leaves_everything_outside_the_root_behind(tmp_path, make):
    """The real sources carry the tool they were packaged with, beside the game.

    Discovery measures only the root, so a copy that forgot to skip the strays would
    land them loose in the new folder — over real files, when the names collide.
    """
    source = make(tmp_path, {**FIXTURE,
                             "downloader/readme.txt": b"packaged by me",
                             "downloader/tool/depot.dll": b"x"})
    destination = tmp_path / "new"
    _materialize(source, destination)
    assert sorted(p.name for p in destination.iterdir()) == [
        "eqclient.ini", "eqgame.exe", "uifiles",
    ]


def test_tree_route_leaves_a_read_only_source_alone_and_copies_writable(tmp_path):
    source = _tree(tmp_path, GAME_FILES)
    read_only = source / "eqclient.ini"
    os.chmod(read_only, stat.S_IREAD)
    destination = tmp_path / "new"
    try:
        _materialize(source, destination)
        assert read_only.exists() and not os.access(read_only, os.W_OK)
        copied = destination / "eqclient.ini"
        assert copied.read_bytes() == FIXTURE["clientfolder/eqclient.ini"]
        # A DVD source would otherwise stamp every copy read-only, and the
        # server's patcher has to write over these.
        assert os.access(copied, os.W_OK)
    finally:
        os.chmod(read_only, stat.S_IWRITE | stat.S_IREAD)


def test_iso_route_strips_the_prefix(tmp_path):
    destination = tmp_path / "new"
    _materialize(_iso(tmp_path), destination)
    assert (destination / "eqgame.exe").read_bytes() == EQGAME
    assert (destination / "uifiles/default/window.xml").read_bytes() == b"<XML/>"
    assert not (destination / "clientfolder").exists()
    assert _temp_dirs(tmp_path) == []


def test_iso_route_keeps_empty_directories_and_zero_byte_files(tmp_path):
    source = _iso(tmp_path, {**FIXTURE, "clientfolder/empty.dat": b""},
                  dirs=("clientfolder/logs/",))
    destination = tmp_path / "new"
    _materialize(source, destination)
    assert (destination / "logs").is_dir()
    assert (destination / "empty.dat").read_bytes() == b""


@pytest.mark.parametrize("flavor", ["udf", "rr"])
def test_iso_route_prefers_a_namespace_that_kept_the_real_names(tmp_path, flavor):
    """UDF and Rock Ridge carry the names as-written; the raw 9660 view is the last resort."""
    destination = tmp_path / "new"
    _materialize(_iso(tmp_path, flavor=flavor), destination)
    assert sorted(p.name for p in destination.iterdir()) == ["eqclient.ini", "eqgame.exe", "uifiles"]


def test_a_raw_iso9660_disc_lands_uppercase_names_without_their_versions(tmp_path):
    """Nothing richer to read: 9660 shouts the names and numbers every version."""
    destination = tmp_path / "new"
    _materialize(_iso(tmp_path, flavor="plain"), destination)
    # iterdir, not exists(): Windows would answer either casing, and ';1' is a legal name.
    assert sorted(p.name for p in destination.iterdir()) == ["EQCLIENT.INI", "EQGAME.EXE", "UIFILES"]


def test_a_garbage_iso_is_refused(tmp_path):
    source = tmp_path / "garbage.iso"
    source.write_bytes(b"not really an iso" * 4096)
    with pytest.raises(provision.ProvisionError, match="Couldn't read"):
        _plan(source)


def test_a_zip_renamed_to_iso_is_refused(tmp_path):
    """Shape comes from the extension, never from sniffing — so this lands in the iso route."""
    with pytest.raises(provision.ProvisionError, match="Couldn't read"):
        _plan(_zip(tmp_path, name="actually_a_zip.iso"))


def test_a_tail_truncated_iso_is_refused_at_scan(tmp_path):
    """An interrupted copy reads clean but short: pycdlib quietly clamps what runs past the end."""
    source = _iso(tmp_path, {"clientfolder/eqgame.exe": b"M" * 300_000})
    source.write_bytes(source.read_bytes()[:-150_000])
    with pytest.raises(provision.ProvisionError, match="shorter than it says"):
        _plan(source)


def test_a_single_bad_record_is_refused_even_when_the_volume_looks_whole(tmp_path, monkeypatch):
    """A padded disc can measure fine and still hold a record pointing past its own end.

    Truncation alone can't show this — the volume check always sees it first — so that
    check stands down here, leaving the per-record one to do the work.
    """
    members = {"clientfolder/eqgame.exe": EQGAME}
    members |= {f"clientfolder/f{i}.dat": b"x" * 40_000 for i in range(6)}
    source = _iso(tmp_path, members)
    source.write_bytes(source.read_bytes()[:63_488])  # far enough in that later extents start past EOF
    monkeypatch.setattr(provision, "_refuse_if_short", lambda source, image: None)
    with pytest.raises(provision.ProvisionError, match="shorter than it says"):
        _plan(source)


def test_a_metadata_truncated_iso_is_refused_rather_than_crashing(tmp_path):
    """Corrupt metadata escapes pycdlib as whatever the bad bytes happen to hit."""
    members = {f"clientfolder/file{i:04d}.dat": b"x" * 2048 for i in range(12)}
    source = _iso(tmp_path, {**members, "clientfolder/eqgame.exe": EQGAME})
    source.write_bytes(source.read_bytes()[:40960])
    with pytest.raises(provision.ProvisionError, match="Couldn't read") as caught:
        _plan(source)
    # The reason the net is cast wide: this one isn't even a pycdlib error. If a
    # future pycdlib raises properly here, tighten the net and delete this line.
    assert not isinstance(caught.value.__cause__, pycdlib.pycdlibexception.PyCdlibException)


def test_a_file_that_reads_short_is_refused(tmp_path, monkeypatch):
    """The scan's size check is the front door; this one watches each file on the way in."""
    real = provision._copy_stream

    def short(reader, writer, label, tick, is_cancelled):
        copied = real(reader, writer, label, tick, is_cancelled)
        return copied - 1 if label == "eqgame.exe" else copied

    monkeypatch.setattr(provision, "_copy_stream", short)
    destination = tmp_path / "new"
    with pytest.raises(provision.ProvisionError, match="damaged"):
        _materialize(_iso(tmp_path), destination)
    assert not destination.exists()
    assert _temp_dirs(tmp_path) == []


def test_a_corrupt_member_leaves_nothing_behind(tmp_path):
    source = tmp_path / "fixture.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("clientfolder/eqgame.exe", b"compress me " * 500)
    raw = bytearray(source.read_bytes())
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("clientfolder/eqgame.exe")
    start = info.header_offset + 30 + len(info.filename)  # past the local file header
    raw[start:start + 8] = b"\x00" * 8
    source.write_bytes(bytes(raw))

    destination = tmp_path / "new"
    with pytest.raises(provision.ProvisionError, match="damaged"):
        _materialize(source, destination)
    assert not destination.exists()
    assert _temp_dirs(tmp_path) == []


@pytest.mark.parametrize("make", [_zip, _iso], ids=["zip", "iso"])
def test_cancelling_mid_copy_leaves_nothing_behind(tmp_path, make):
    source = make(tmp_path, {f"clientfolder/file{i}.dat": b"x" * 4096 for i in range(8)}
                  | {"clientfolder/eqgame.exe": EQGAME})
    destination = tmp_path / "new"
    polls = []

    def cancelled():
        polls.append(1)
        return len(polls) > 3  # a few files in, so real bytes are on disk

    with pytest.raises(provision.ProvisionCancelled):
        _materialize(source, destination, cancelled=cancelled)
    assert not destination.exists()
    assert _temp_dirs(tmp_path) == []


def test_progress_fractions_climb_to_one(tmp_path):
    seen = []
    _materialize(_zip(tmp_path), tmp_path / "new",
                 progress=lambda label, fraction: seen.append(fraction))
    fractions = [f for f in seen if f is not None]
    assert fractions == sorted(fractions)
    assert fractions[-1] == pytest.approx(1.0)


def test_progress_ticks_are_throttled_but_the_final_one_lands(monkeypatch):
    monkeypatch.setattr(provision.time, "monotonic", lambda: 1000.0)
    seen = []
    tick = provision._ticker(4, lambda label, fraction: seen.append(fraction))
    for _ in range(4):
        tick("copying", 1)
    assert seen == [0.25, 1.0]


def test_stale_work_dirs_are_swept_but_fresh_ones_survive(tmp_path):
    stale = tmp_path / f"{provision.TEMP_PREFIX}old"
    stale.mkdir()
    os.utime(stale, (0, 0))
    fresh = tmp_path / f"{provision.TEMP_PREFIX}live"
    fresh.mkdir()

    _materialize(_zip(tmp_path), tmp_path / "new")
    assert not stale.exists()
    assert fresh.exists()


def test_an_existing_empty_destination_is_replaced(tmp_path):
    destination = tmp_path / "new"
    destination.mkdir()
    _materialize(_zip(tmp_path), destination)
    assert (destination / "eqgame.exe").read_bytes() == EQGAME


def test_a_rename_blocked_by_an_open_handle_is_retried(tmp_path, monkeypatch):
    """Windows refuses a directory rename while an AV sweep holds a child handle."""
    calls = []
    real_rename = os.rename

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "Access is denied")
        real_rename(src, dst)

    monkeypatch.setattr(provision.os, "rename", flaky)
    monkeypatch.setattr(provision._rename_with_retry.retry, "sleep", lambda _s: None)
    destination = tmp_path / "new"
    _materialize(_zip(tmp_path), destination)
    assert len(calls) == 3
    assert (destination / "eqgame.exe").read_bytes() == EQGAME


# --- the finish chain --------------------------------------------------------

@pytest.fixture
def chain(monkeypatch):
    """Record the finish chain's steps in the order they run."""
    calls = []
    ctx = servers.ServerContext(
        label="Project Lazarus",
        eqpath="",
        patcher_url="https://laz.example.test/patcher.zip",
        patcher_exe="LazarusPatcherCLI.exe",
    )

    async def fake_install(context):
        calls.append(("patcher", context.eqpath))

    monkeypatch.setattr(servers, "list_servers", lambda env: {"lazarus": {}})
    monkeypatch.setattr(
        servers, "server_context",
        lambda slug, env, *, eqpath: dataclasses.replace(ctx, eqpath=eqpath),
    )
    monkeypatch.setattr(patcher, "install", fake_install)
    monkeypatch.setattr(laa, "enable", lambda path: calls.append(("laa", path)))
    monkeypatch.setattr(servers, "add_server", lambda slug, **kw: calls.append(("add", kw["eqpath"])))
    monkeypatch.setattr(utils, "dx9_missing", lambda: False)
    return calls


def test_the_chain_runs_in_order_and_registers_last(tmp_path, chain):
    result = _provision(tmp_path)
    # The order the Add dialog discloses up front, LAA last so it flags the patcher's exe.
    assert [step for step, _ in chain] == ["patcher", "laa", "add"]
    assert result.destination == tmp_path / "new"
    assert result.notices == ()
    assert (tmp_path / "new" / "eqgame.exe").is_file()


def test_the_move_is_reported_so_callers_can_stop_offering_cancel(tmp_path, chain):
    """Past this point the chain registers regardless, so cancelling is off the table."""
    steps = []
    asyncio.run(provision.provision(
        "lazarus", env="EMU", source=str(_zip(tmp_path)),
        destination=str(tmp_path / "new"),
        progress=lambda label, fraction: steps.append(label),
    ))
    assert provision.FINISHING_LABEL in steps
    # Everything the finish chain reports comes after it.
    assert steps.index(provision.FINISHING_LABEL) < len(steps) - 1


def test_a_custom_server_provisions_from_the_values_the_caller_collected(tmp_path, monkeypatch):
    """The dialog's custom mode has no bundle entry to read a patcher from."""
    calls = []
    monkeypatch.setattr(servers, "list_servers", lambda env: {})
    monkeypatch.setattr(servers, "validate_server_slug",
                        lambda slug, must_be_new=False: slug)
    monkeypatch.setattr(laa, "enable", lambda path: None)
    monkeypatch.setattr(utils, "dx9_missing", lambda: False)
    monkeypatch.setattr(servers, "add_server", lambda slug, **kw: calls.append(("add", kw)))

    async def fake_install(context):
        calls.append(("patcher", context.patcher_url))

    monkeypatch.setattr(patcher, "install", fake_install)

    asyncio.run(provision.provision(
        "thegrind", env="EMU", source=str(_zip(tmp_path)),
        destination=str(tmp_path / "new"),
        label="The Grind", patcher_url="https://grind.example.test/p.zip",
        patcher_exe="GrindPatcher.exe",
    ))

    assert ("patcher", "https://grind.example.test/p.zip") in calls
    added = dict(calls[-1][1])
    assert added["label"] == "The Grind"
    assert added["patcher_exe"] == "GrindPatcher.exe"


def test_a_taken_custom_slug_is_refused_before_anything_is_copied(tmp_path, monkeypatch):
    """add_server would only catch it after the copy, and as a ValueError."""
    monkeypatch.setattr(servers, "list_servers", lambda env: {})

    def taken(slug, must_be_new=False):
        raise ValueError(f"Server name '{slug}' is already in use on Live.")

    monkeypatch.setattr(servers, "validate_server_slug", taken)
    with pytest.raises(provision.ProvisionError, match="already in use"):
        asyncio.run(provision.provision(
            "thegrind", env="EMU", source=str(_zip(tmp_path)),
            destination=str(tmp_path / "new"),
        ))
    assert not (tmp_path / "new").exists()


def test_a_patcher_failure_degrades_to_a_notice(tmp_path, chain, monkeypatch):
    async def failing(_ctx):
        raise patcher.PatcherError("the website is blocking your connection")

    monkeypatch.setattr(patcher, "install", failing)
    result = _provision(tmp_path)
    assert "blocking your connection" in result.notices[0]
    assert [step for step, _ in chain] == ["laa", "add"]  # registered anyway


def test_provision_refuses_an_occupied_destination(tmp_path, chain):
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "eqgame.exe").write_bytes(EQGAME)
    with pytest.raises(provision.ProvisionError, match="use an existing folder"):
        _provision(tmp_path, destination=destination)
    assert chain == []


def test_provision_refuses_a_volume_without_room(tmp_path, chain, monkeypatch):
    monkeypatch.setattr(provision.shutil, "disk_usage", lambda _path: _usage(free=100))
    with pytest.raises(provision.ProvisionError, match="enough room"):
        _provision(tmp_path)
    assert chain == []
    assert not (tmp_path / "new").exists()


def test_provision_refuses_an_empty_source_without_reading_the_cwd(tmp_path, chain):
    """Path('') is '.', which classifies as a tree rooted at the working directory."""
    with pytest.raises(provision.ProvisionError) as caught:
        _provision(tmp_path, source="")
    assert str(caught.value) == provision.NO_SOURCE_MESSAGE
    assert chain == []


def test_a_failed_copy_registers_nothing(tmp_path, chain, monkeypatch):
    def explode(*args, **kwargs):
        raise provision.ProvisionError("the copy died")

    monkeypatch.setattr(provision, "materialize", explode)
    with pytest.raises(provision.ProvisionError, match="the copy died"):
        _provision(tmp_path)
    assert chain == []


def test_a_server_with_no_patcher_skips_that_step(tmp_path, chain, monkeypatch):
    monkeypatch.setattr(
        servers, "server_context",
        lambda slug, env, *, eqpath: servers.ServerContext(label="Custom", eqpath=eqpath),
    )
    _provision(tmp_path)
    assert [step for step, _ in chain] == ["laa", "add"]  # nothing to install


def test_a_folder_without_eqgame_is_never_registered(tmp_path, chain, monkeypatch):
    monkeypatch.setattr(detecteq, "is_valid_eq_dir", lambda path: False)
    with pytest.raises(provision.ProvisionError, match="wasn't added as a server"):
        _provision(tmp_path)
    assert "add" not in [step for step, _ in chain]


# --- servers.server_context --------------------------------------------------

def test_server_context_reads_the_bundle_at_a_chosen_folder(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch)
    ctx = servers.server_context("lazarus", "EMU", eqpath=str(tmp_path / "new"))
    assert ctx.label == "Project Lazarus"
    assert ctx.eqpath == str(tmp_path / "new")
    assert ctx.patcher_exe == "LazarusPatcherCLI.exe"


def test_server_context_refuses_an_unknown_slug(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Unknown server"):
        servers.server_context("nosuchserver", "EMU", eqpath=str(tmp_path))


# --- the CLI runner ----------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, tmp_path):
    """provision_command with its config reads and the chain itself stubbed out."""
    calls = []
    monkeypatch.setattr(config, "initialize_config", lambda: None)
    monkeypatch.setattr(servers, "list_servers", lambda env: {"lazarus": {}})
    monkeypatch.setattr(servers, "is_known_server", lambda slug, env: slug == "lazarus")
    monkeypatch.setattr(servers, "is_server_configured", lambda slug, env: False)
    monkeypatch.setattr(provision, "clean_source", lambda: "")
    monkeypatch.setattr(provision, "set_clean_source", lambda path: calls.append(("remember", path)))
    monkeypatch.setattr(
        provision, "default_destination", lambda slug: str(tmp_path / f"EverQuest_{slug}")
    )

    async def fake_provision(slug, *, env, source, destination, progress=None, cancelled=None):
        calls.append(("provision", slug, env, source, destination))
        return provision.ProvisionResult(Path(destination), ())

    monkeypatch.setattr(provision, "provision", fake_provision)
    return calls


def test_cli_refuses_a_server_it_doesnt_know(cli):
    with pytest.raises(typer.BadParameter, match="isn't a known emu server"):
        main.provision_command(server="nosuchserver", source=None, destination=None)
    assert cli == []


def test_cli_refuses_a_server_that_already_has_a_folder(cli, monkeypatch):
    monkeypatch.setattr(servers, "is_server_configured", lambda slug, env: True)
    with pytest.raises(typer.Exit) as caught:
        main.provision_command(server="lazarus", source=None, destination=None)
    assert caught.value.exit_code == 1
    assert cli == []


def test_cli_asks_for_a_source_without_naming_where_to_get_one(cli, capsys):
    with pytest.raises(typer.Exit):
        main.provision_command(server="lazarus", source=None, destination=None)
    printed = capsys.readouterr().out
    # Against the constant, not a quoted phrase, so rewording it can't redden this;
    # on collapsed whitespace, since rich wraps the line at console width.
    assert " ".join(provision.NO_SOURCE_MESSAGE.split()) in " ".join(printed.split())
    # The sourcing story: what to have, never where to get it.
    assert "http" not in printed and "download" not in printed.lower()
    assert cli == []


def test_cli_remembers_a_source_it_was_handed(cli, tmp_path):
    source = str(_zip(tmp_path))
    destination = str(tmp_path / "new")
    main.provision_command(server="lazarus", source=source, destination=destination)
    assert ("remember", source) in cli
    assert ("provision", "lazarus", "EMU", source, destination) in cli


def test_cli_leaves_an_already_remembered_source_alone(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(provision, "clean_source", lambda: str(tmp_path / "remembered.zip"))
    main.provision_command(
        server="lazarus", source=str(_zip(tmp_path, name="other.zip")),
        destination=str(tmp_path / "new"),
    )
    assert not any(step == "remember" for step, *_rest in cli)


def test_cli_falls_back_to_the_remembered_source_and_default_folder(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(provision, "clean_source", lambda: "remembered.zip")
    main.provision_command(server="lazarus", source=None, destination=None)
    assert cli[-1] == (
        "provision", "lazarus", "EMU", "remembered.zip", str(tmp_path / "EverQuest_lazarus")
    )


def test_cli_prints_the_switch_hint_and_any_notices(cli, tmp_path, monkeypatch, capsys):
    async def with_notices(slug, *, env, source, destination, progress=None, cancelled=None):
        return provision.ProvisionResult(Path(destination), ("the patcher didn't run",))

    monkeypatch.setattr(provision, "clean_source", lambda: "remembered.zip")
    monkeypatch.setattr(provision, "provision", with_notices)
    main.provision_command(server="lazarus", source=None, destination=str(tmp_path / "new"))
    printed = capsys.readouterr().out
    assert "the patcher didn't run" in printed
    assert "redfetch server lazarus" in printed


def test_cli_styles_a_cancel_as_a_notice(cli, monkeypatch, capsys):
    async def cancelling(*args, **kwargs):
        raise provision.ProvisionCancelled("Setup was cancelled, so nothing was created.")

    monkeypatch.setattr(provision, "clean_source", lambda: "remembered.zip")
    monkeypatch.setattr(provision, "provision", cancelling)
    with pytest.raises(typer.Exit) as caught:
        main.provision_command(server="lazarus", source=None, destination=None)
    assert caught.value.exit_code == 1
    assert "cancelled" in capsys.readouterr().out.lower()
