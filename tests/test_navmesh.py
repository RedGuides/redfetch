"""Tests for navmesh manifest sourcing and sync selection."""
import asyncio
import hashlib
import os
import sqlite3

from conftest import _install_settings
from redfetch import navmesh


MQMESH_URL = "https://mqmesh.com/updater.json"


# --- get_manifest_url --------------------------------------------------------

def test_live_and_test_default_to_mqmesh(tmp_path, monkeypatch):
    for env in ("LIVE", "TEST"):
        _install_settings(tmp_path, monkeypatch, env=env)
        assert navmesh.get_manifest_url() == MQMESH_URL


def test_emu_defaults_to_its_own_manifest(tmp_path, monkeypatch):
    """EMU repoints away from live mqmesh — its geometry is wrong for RoF2."""
    _install_settings(tmp_path, monkeypatch, env="EMU")
    url = navmesh.get_manifest_url()
    assert url.startswith("https://")
    assert url != MQMESH_URL


OVERRIDE_LOCAL = """
[EMU]
EQPATH = "D:/EQ-Custom"
ACTIVE_SERVER = "custommesh"

[EMU.SERVERS.custommesh]
label = "Custom Mesh"
opt_in = true
eqpath = "D:/EQ-Custom"
navmesh_manifest = "https://meshes.example/updater.json"
"""


def test_server_override_replaces_env_default(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=OVERRIDE_LOCAL, env="EMU")
    assert navmesh.get_manifest_url() == "https://meshes.example/updater.json"


def test_active_server_without_override_uses_env_default(tmp_path, monkeypatch):
    local = """
[EMU]
EQPATH = "D:/EQ-Plain"
ACTIVE_SERVER = "plain"

[EMU.SERVERS.plain]
label = "Plain"
opt_in = true
eqpath = "D:/EQ-Plain"
"""
    settings = _install_settings(tmp_path, monkeypatch, local_toml=local, env="EMU")
    assert navmesh.get_manifest_url() == settings.from_env("EMU").get("NAVMESH_MANIFEST")


def test_local_settings_can_repoint_the_env(tmp_path, monkeypatch):
    local = '[EMU]\nNAVMESH_MANIFEST = "https://mine.example/updater.json"\n'
    _install_settings(tmp_path, monkeypatch, local_toml=local, env="EMU")
    assert navmesh.get_manifest_url() == "https://mine.example/updater.json"


# --- sync_navmeshes ----------------------------------------------------------

def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_db(tmp_path) -> str:
    db_path = str(tmp_path / "navmesh_test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE navmesh_files (
                filename TEXT PRIMARY KEY,
                md5_hash TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL
            )
            """
        )
    return db_path


def _manifest_for(zones: dict[str, bytes]) -> dict:
    return {
        "zones": {
            zone: {"files": {"mesh": {
                "link": f"https://meshes.example/{zone}.navmesh",
                # uppercase: the compare must be case-insensitive
                "hash": _md5(content).upper(),
                "size": "1.00MB",
            }}}
            for zone, content in zones.items()
        }
    }


def _install_sync_env(tmp_path, monkeypatch, local_toml=""):
    """Real EMU settings plus a navmesh dir under a fake VVMQ path."""
    _install_settings(tmp_path, monkeypatch, local_toml=local_toml, env="EMU")
    vvmq = tmp_path / "VVMQ"
    monkeypatch.setattr(navmesh, "get_vvmq_path", lambda: str(vvmq))
    navmesh_dir = vvmq / "resources" / "MQ2Nav"
    navmesh_dir.mkdir(parents=True)
    return navmesh_dir


def _patch_network(monkeypatch, manifest, remote_bytes, downloaded):
    """Serve the fixture manifest and bytes without touching the network."""
    async def fake_fetch(db_path, url):
        return manifest

    async def fake_download(client, url, file_path, expected_md5=None):
        zone = os.path.basename(file_path).removesuffix(".navmesh")
        with open(file_path, "wb") as f:
            f.write(remote_bytes[zone])
        downloaded.append(os.path.basename(file_path))
        return True

    monkeypatch.setattr(navmesh, "fetch_manifest", fake_fetch)
    monkeypatch.setattr(navmesh, "download_file_async", fake_download)


def test_manifest_drives_the_download_list(tmp_path, monkeypatch):
    """Only hash-mismatched meshes download; malformed zone entries are ignored."""
    navmesh_dir = _install_sync_env(tmp_path, monkeypatch)
    db_path = _make_db(tmp_path)

    remote = {"gfaydark": b"gfay-mesh-v2", "crushbone": b"crushbone-mesh-v1"}
    manifest = _manifest_for(remote)
    manifest["zones"]["nomesh"] = {"files": {}}
    manifest["zones"]["nohash"] = {"files": {"mesh": {"link": "https://meshes.example/nohash.navmesh"}}}
    # gfaydark is already current on disk; crushbone is missing.
    (navmesh_dir / "gfaydark.navmesh").write_bytes(remote["gfaydark"])

    downloaded: list[str] = []
    _patch_network(monkeypatch, manifest, remote, downloaded)
    events: list[tuple] = []

    ok = asyncio.run(navmesh.sync_navmeshes(db_path, on_event=events.append))

    assert ok is True
    assert downloaded == ["crushbone.navmesh"]
    assert (navmesh_dir / "crushbone.navmesh").read_bytes() == remote["crushbone"]
    assert ("add_total", 1, None) in events
    assert ("done", "crushbone.navmesh", "downloaded") in events
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT md5_hash FROM navmesh_files WHERE filename = 'crushbone.navmesh'"
        ).fetchone()
    assert row == (_md5(remote["crushbone"]),)  # stored lowercased


def test_protected_meshes_are_never_replaced(tmp_path, monkeypatch):
    local = """
[EMU.PROTECTED_FILES_BY_RESOURCE]
navmesh = ["gfaydark.navmesh"]
"""
    navmesh_dir = _install_sync_env(tmp_path, monkeypatch, local_toml=local)
    db_path = _make_db(tmp_path)

    remote = {"gfaydark": b"gfay-mesh-v2"}
    (navmesh_dir / "gfaydark.navmesh").write_bytes(b"my hand-tuned mesh")

    downloaded: list[str] = []
    _patch_network(monkeypatch, _manifest_for(remote), remote, downloaded)

    ok = asyncio.run(navmesh.sync_navmeshes(db_path))

    assert ok is True
    assert downloaded == []
    assert (navmesh_dir / "gfaydark.navmesh").read_bytes() == b"my hand-tuned mesh"


def test_blank_manifest_url_skips_without_fetching(tmp_path, monkeypatch):
    _install_sync_env(tmp_path, monkeypatch, local_toml='[EMU]\nNAVMESH_MANIFEST = ""\n')
    db_path = _make_db(tmp_path)

    async def boom(db_path, url):
        raise AssertionError("fetch_manifest must not be called without a URL")

    monkeypatch.setattr(navmesh, "fetch_manifest", boom)

    assert asyncio.run(navmesh.sync_navmeshes(db_path)) is True
