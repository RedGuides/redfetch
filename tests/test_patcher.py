"""Patcher bootstrap: exe-name validation, the download routes, and failure shapes."""
import asyncio
import inspect
import io
import os
import zipfile

import httpx
import pytest

from redfetch import download, net, patcher, servers


URL_ZIP = "https://laz.example.test/lazarus-patcher-windows.zip"
URL_EXE = "https://laz.example.test/patcher.exe"
EXE = "LazarusPatcherCLI.exe"


# --- fixtures ----------------------------------------------------------------

def _ctx(tmp_path, *, url=URL_ZIP, exe=EXE, guide="https://laz.example.test/guide", eqpath=None):
    return servers.ServerContext(
        label="Project Lazarus",
        eqpath=str(tmp_path) if eqpath is None else eqpath,
        patcher_url=url,
        patcher_exe=exe,
        guide=guide,
    )


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _zip_with_a_corrupt_member() -> bytes:
    """A well-formed archive whose member data is damaged — fails mid-extraction."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(EXE, b"MZ" + b"compress me " * 200)
    raw = bytearray(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
        info = archive.getinfo(EXE)
    start = info.header_offset + 30 + len(EXE)  # past the local file header
    raw[start:start + 8] = b"\x00" * 8
    return bytes(raw)


def _serve(monkeypatch, handler):
    """Point the bootstrap's unauth client at a MockTransport, recording requests."""
    requests: list[httpx.Request] = []

    def recording(request):
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        net,
        "new_unauth_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording), follow_redirects=True),
    )
    return requests


def _install(ctx):
    return asyncio.run(patcher.install(ctx))


def _work_dirs(tmp_path):
    return [name for name in os.listdir(tmp_path) if name.startswith(patcher.TEMP_PREFIX)]


# --- exe name validation --------------------------------------------------------

@pytest.mark.parametrize("name", ["LazarusPatcherCLI.exe", "patcher.exe", "EQAscendant.exe", "a"])
def test_bare_filenames_pass(name):
    assert patcher.validate_patcher_exe(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        None,
        "sub/patcher.exe",
        "sub\\patcher.exe",
        "/patcher.exe",
        "..",
        ".",
        "../patcher.exe",
        "C:patcher.exe",         # drive-relative: resolves against the CWD on that drive
        "C:\\Windows\\evil.exe",
        "patcher.exe:stream",    # NTFS alternate data stream
        "patcher.exe.",          # Windows drops the trailing dot
        "patcher.exe ",
        " patcher.exe",
        "patch*er.exe",
        "patcher\n.exe",
        "CON",                   # reserved device names, extension or not
        "con.exe",
        "NUL.txt",
    ],
)
def test_non_bare_filenames_rejected(name):
    with pytest.raises(patcher.PatcherError):
        patcher.validate_patcher_exe(name)


# --- gates ---------------------------------------------------------------------

def test_has_patcher_needs_both_url_and_exe(tmp_path):
    assert patcher.has_patcher(_ctx(tmp_path)) is True
    assert patcher.has_patcher(_ctx(tmp_path, url="")) is False
    assert patcher.has_patcher(_ctx(tmp_path, exe="")) is False


def test_is_installed_tracks_the_exe_on_disk(tmp_path):
    ctx = _ctx(tmp_path)
    assert patcher.is_installed(ctx) is False
    (tmp_path / EXE).write_bytes(b"MZ")
    assert patcher.is_installed(ctx) is True


def test_is_installed_is_false_for_unusable_entries(tmp_path):
    """The UI calls this on every row; bad data must not raise at it."""
    assert patcher.is_installed(_ctx(tmp_path, exe="sub/evil.exe")) is False
    assert patcher.is_installed(_ctx(tmp_path, eqpath="")) is False


def test_install_short_circuits_when_already_installed(tmp_path, monkeypatch):
    requests = _serve(monkeypatch, lambda r: httpx.Response(500))
    (tmp_path / EXE).write_bytes(b"MZ")

    assert _install(_ctx(tmp_path)) == tmp_path / EXE
    assert requests == []  # the exe on disk is the completion marker


def test_install_without_a_patcher_configured(tmp_path, monkeypatch):
    requests = _serve(monkeypatch, lambda r: httpx.Response(200))
    with pytest.raises(patcher.PatcherError, match="doesn't have a patcher"):
        _install(_ctx(tmp_path, url=""))
    assert requests == []


def test_install_rejects_a_missing_eq_folder(tmp_path, monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200))
    ctx = _ctx(tmp_path, eqpath=str(tmp_path / "gone"))
    with pytest.raises(patcher.PatcherError, match="isn't there"):
        _install(ctx)


# --- the zip route ---------------------------------------------------------------

def test_zip_installs_payload_into_the_eq_folder(tmp_path, monkeypatch):
    payload = _zip_bytes({EXE: b"MZ-patcher", "readme.txt": b"hello"})
    requests = _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    installed = _install(_ctx(tmp_path))

    assert installed == tmp_path / EXE
    assert (tmp_path / EXE).read_bytes() == b"MZ-patcher"
    assert (tmp_path / "readme.txt").read_bytes() == b"hello"  # companions ride along
    assert len(requests) == 1
    assert _work_dirs(tmp_path) == []  # the work dir is always cleaned up


def test_zip_route_sends_no_credentials(tmp_path, monkeypatch):
    """Nothing on the call path bolts credentials onto the request."""
    requests = _serve(monkeypatch, lambda r: httpx.Response(200, content=_zip_bytes({EXE: b"MZ"})))

    _install(_ctx(tmp_path))

    assert "authorization" not in {key.lower() for key in requests[0].headers}


def test_the_unauth_client_itself_carries_nothing_of_ours():
    """The tests above swap the factory out, so pin the real one: its missing
    `headers` parameter is the whole guarantee."""
    assert "headers" not in inspect.signature(net.new_unauth_client).parameters
    client = net.new_unauth_client()
    try:
        assert {"authorization", "cookie"}.isdisjoint({k.lower() for k in client.headers})
        assert client.follow_redirects is True  # third-party hosts hand off to CDNs
    finally:
        asyncio.run(client.aclose())


def test_zip_without_the_promised_exe_installs_nothing(tmp_path, monkeypatch):
    payload = _zip_bytes({"SomethingElse.exe": b"MZ", "readme.txt": b"hello"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    with pytest.raises(patcher.PatcherError, match="didn't contain"):
        _install(_ctx(tmp_path))

    assert os.listdir(tmp_path) == []  # verified before the move, so nothing landed


def test_corrupt_zip_installs_nothing(tmp_path, monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, content=b"PK\x03\x04 not really a zip"))

    with pytest.raises(patcher.PatcherError, match="corrupt"):
        _install(_ctx(tmp_path))

    assert os.listdir(tmp_path) == []


def test_zip_slip_member_cannot_escape(tmp_path, monkeypatch):
    """The realistic attack: two levels up from payload/ is outside the EQ folder.

    extractall strips the ".." components, so the member lands inside the EQ
    folder like any companion file — never above it.
    """
    eq_dir = tmp_path / "eq"
    eq_dir.mkdir()
    payload = _zip_bytes({"../../evil.dll": b"pwned", EXE: b"MZ"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    _install(_ctx(eq_dir))

    assert not (tmp_path / "evil.dll").exists()  # nothing above the EQ folder
    assert os.listdir(tmp_path) == ["eq"]
    assert (eq_dir / "evil.dll").read_bytes() == b"pwned"  # defanged into a companion


@pytest.mark.skipif(os.name != "nt", reason="drive-absolute names only escape on Windows")
def test_drive_absolute_zip_member_cannot_escape(tmp_path, monkeypatch):
    payload = _zip_bytes({"C:/Windows/Temp/evil.dll": b"pwned", EXE: b"MZ"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    _install(_ctx(tmp_path))

    # extractall strips the drive, so the member lands under the EQ folder.
    assert (tmp_path / "Windows" / "Temp" / "evil.dll").read_bytes() == b"pwned"


def test_corrupt_member_installs_nothing(tmp_path, monkeypatch):
    """Corruption inside a member raises zlib.error, not BadZipFile — both must land as PatcherError."""
    _serve(monkeypatch, lambda r: httpx.Response(200, content=_zip_with_a_corrupt_member()))

    with pytest.raises(patcher.PatcherError, match="corrupt"):
        _install(_ctx(tmp_path))

    assert os.listdir(tmp_path) == []


def test_the_exe_is_the_last_thing_moved(tmp_path, monkeypatch):
    """The exe is the completion marker, so it must never land before its companions."""
    landed = []
    real_replace = os.replace

    def recording(source, destination):
        if os.path.normcase(os.path.dirname(destination)) == os.path.normcase(str(tmp_path)):
            landed.append(os.path.basename(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(patcher.os, "replace", recording)
    payload = _zip_bytes({"aaa.txt": b"a", EXE: b"MZ", "zzz.txt": b"z"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    _install(_ctx(tmp_path))

    assert landed[-1] == EXE
    assert sorted(landed[:-1]) == ["aaa.txt", "zzz.txt"]


def test_a_failed_move_is_reported_and_leaves_no_marker(tmp_path, monkeypatch):
    real_replace = os.replace

    def locked(source, destination):
        if os.path.basename(destination) == "readme.txt":
            raise PermissionError(13, "held open by antivirus")
        return real_replace(source, destination)

    monkeypatch.setattr(patcher.os, "replace", locked)
    _serve(monkeypatch, lambda r: httpx.Response(200, content=_zip_bytes({EXE: b"MZ", "readme.txt": b"x"})))

    with pytest.raises(patcher.PatcherError, match="Couldn't put"):
        _install(_ctx(tmp_path))

    assert not (tmp_path / EXE).exists()  # the marker never lands on a failed move
    assert _work_dirs(tmp_path) == []


def test_install_overwrites_leftovers_from_a_partial_move(tmp_path, monkeypatch):
    """Starting over is the recovery path, so companions from a dead run get replaced."""
    (tmp_path / "readme.txt").write_bytes(b"stale")
    _serve(monkeypatch, lambda r: httpx.Response(200, content=_zip_bytes({EXE: b"MZ", "readme.txt": b"fresh"})))

    _install(_ctx(tmp_path))

    assert (tmp_path / "readme.txt").read_bytes() == b"fresh"
    assert (tmp_path / EXE).read_bytes() == b"MZ"


def test_zip_with_a_nested_folder_installs_it(tmp_path, monkeypatch):
    """Custom servers may ship a structured archive; both bundled ones are flat."""
    _serve(monkeypatch, lambda r: httpx.Response(
        200, content=_zip_bytes({EXE: b"MZ", "resources/data.txt": b"x"})
    ))

    _install(_ctx(tmp_path))

    assert (tmp_path / "resources" / "data.txt").read_bytes() == b"x"
    assert (tmp_path / EXE).exists()


def test_an_unwritable_eq_folder_is_reported(tmp_path, monkeypatch):
    """install() promises a PatcherError for every failure — mkdtemp included."""
    def refuse(*args, **kwargs):
        raise PermissionError(13, "access is denied")

    monkeypatch.setattr(patcher.tempfile, "mkdtemp", refuse)
    _serve(monkeypatch, lambda r: httpx.Response(200, content=_zip_bytes({EXE: b"MZ"})))

    with pytest.raises(patcher.PatcherError, match="Couldn't write to"):
        _install(_ctx(tmp_path))


def test_the_unpack_thread_owns_its_work_dir(tmp_path):
    """Cleanup belongs to the thread: cancelling the await can't stop a live move,
    so a caller-side finally would rmtree the folder out from under it."""
    work_dir = tmp_path / "work"
    (work_dir / "payload").mkdir(parents=True)

    with pytest.raises(patcher.PatcherError):
        patcher._unpack_and_clean(
            _ctx(tmp_path), work_dir / "missing.zip", work_dir, tmp_path / EXE
        )

    assert not work_dir.exists()


def test_a_download_that_reports_failure_is_surfaced(tmp_path, monkeypatch):
    """download_file_async returns False (not raises) for a locked target or bad md5."""
    async def reports_failure(client, url, path, expected_md5=None):
        return False

    monkeypatch.setattr(download, "download_file_async", reports_failure)
    _serve(monkeypatch, lambda r: httpx.Response(200))

    for url in (URL_ZIP, URL_EXE):
        with pytest.raises(patcher.PatcherError, match="didn't finish"):
            _install(_ctx(tmp_path, url=url))
    assert _work_dirs(tmp_path) == []


def test_zip_bomb_caps_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "MAX_FILES_PER_ZIP", 1)
    payload = _zip_bytes({EXE: b"MZ", "extra.txt": b"x"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    with pytest.raises(patcher.PatcherError, match="too many files"):
        _install(_ctx(tmp_path))

    assert os.listdir(tmp_path) == []


def test_zip_does_not_replace_an_existing_folder(tmp_path, monkeypatch):
    (tmp_path / "resources").mkdir()
    payload = _zip_bytes({EXE: b"MZ", "resources/data.txt": b"x"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=payload))

    with pytest.raises(patcher.PatcherError, match="already in"):
        _install(_ctx(tmp_path))

    assert not (tmp_path / EXE).exists()  # the exe moves last, so it never landed


def test_stale_work_dirs_are_swept_and_nothing_else(tmp_path, monkeypatch):
    """The prefix check is all that stands between the sweep and a whole EQ install."""
    stale = tmp_path / f"{patcher.TEMP_PREFIX}old"
    stale.mkdir()
    (stale / "junk.bin").write_bytes(b"x")
    fresh = tmp_path / f"{patcher.TEMP_PREFIX}live"
    fresh.mkdir()
    # An aged, unrelated EQ folder and file: the rmtree must never reach these.
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "nektulos.txt").write_bytes(b"map")
    eqgame = tmp_path / "eqgame.exe"
    eqgame.write_bytes(b"MZ-game")
    for aged in (stale, maps, eqgame):
        os.utime(aged, (0, 0))  # older than the sweep cutoff
    _serve(monkeypatch, lambda r: httpx.Response(200, content=_zip_bytes({EXE: b"MZ"})))

    _install(_ctx(tmp_path))

    assert not stale.exists()
    assert fresh.exists()  # a concurrent run's dir is left alone
    assert (maps / "nektulos.txt").read_bytes() == b"map"
    assert eqgame.read_bytes() == b"MZ-game"


# --- the bare-exe route -----------------------------------------------------------

def test_bare_exe_url_downloads_straight_to_the_folder(tmp_path, monkeypatch):
    requests = _serve(monkeypatch, lambda r: httpx.Response(200, content=b"MZ-direct"))

    installed = _install(_ctx(tmp_path, url=URL_EXE))

    assert installed == tmp_path / EXE
    assert (tmp_path / EXE).read_bytes() == b"MZ-direct"
    assert len(requests) == 1
    assert _work_dirs(tmp_path) == []  # no work dir needed at all


def test_sfx_exe_is_never_unzipped(tmp_path, monkeypatch):
    """A self-extracting installer is a zipfile; the entry data decides, not is_zipfile()."""
    sfx = _zip_bytes({"inner.txt": b"payload"})
    _serve(monkeypatch, lambda r: httpx.Response(200, content=sfx))

    _install(_ctx(tmp_path, url=URL_EXE))

    assert (tmp_path / EXE).read_bytes() == sfx  # installed whole, not torn open
    assert not (tmp_path / "inner.txt").exists()


@pytest.mark.parametrize(
    "url, expect_zip",
    [
        ("https://h.test/p.zip", True),
        ("https://h.test/P.ZIP", True),
        ("https://h.test/p.zip?v=3", True),
        ("https://h.test/download?file=p.zip", False),  # the path is what counts
        ("https://h.test/patcher.exe", False),
        ("https://h.test/download", False),
    ],
)
def test_zip_route_is_chosen_from_the_url_path(url, expect_zip):
    assert patcher._is_zip_url(url) is expect_zip


# --- failure messages ---------------------------------------------------------------

def test_403_blames_the_server_and_links_its_guide(tmp_path, monkeypatch):
    requests = _serve(
        monkeypatch,
        lambda r: httpx.Response(403, headers={"cf-mitigated": "challenge"}),
    )

    with pytest.raises(patcher.PatcherError) as excinfo:
        _install(_ctx(tmp_path))

    message = str(excinfo.value)
    assert "The Project Lazarus website is blocking your connection" in message
    assert "https://laz.example.test/guide" in message
    assert len(requests) == 1  # a permanent 4xx is not retried
    assert _work_dirs(tmp_path) == []


def test_403_without_a_guide_still_reads_cleanly(tmp_path, monkeypatch):
    """Custom servers have no guide key."""
    _serve(monkeypatch, lambda r: httpx.Response(403))

    with pytest.raises(patcher.PatcherError) as excinfo:
        _install(_ctx(tmp_path, guide=""))

    message = str(excinfo.value)
    assert "blocking your connection" in message
    assert "setup guide" not in message


def test_other_status_errors_name_the_code(tmp_path, monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(404))

    with pytest.raises(patcher.PatcherError, match="returned an error \\(404\\)"):
        _install(_ctx(tmp_path))


def test_network_failure_is_reported_as_unreachable(tmp_path, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    _serve(monkeypatch, boom)

    with pytest.raises(patcher.PatcherError, match="Couldn't reach"):
        _install(_ctx(tmp_path))

    assert _work_dirs(tmp_path) == []
