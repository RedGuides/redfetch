"""Tests for emulator server profiles and switching."""
import os
import re
import tomllib

import pytest
from dynaconf import Dynaconf

from redfetch import config


# --- fixtures ----------------------------------------------------------------

# Stable bundled-server fixture for merge tests.
BUNDLE_WITH_KNOWN = """
[EMU.SERVERS.thegrind]
label = "The Grind"
opt_in = false
patcher_url = "https://thegrind.example/patcher.zip"
patcher_exe = "patcher.exe"
"""


def _install_settings(tmp_path, monkeypatch, local_toml="", bundle_toml=None, env="EMU",
                      current_env=None):
    """Install a real Dynaconf instance backed by temporary settings files."""
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    if current_env:
        monkeypatch.setenv("REDFETCH_ENV", current_env)
    if bundle_toml is None:
        bundle = os.path.join(config.script_dir, "settings.toml")
    else:
        bundle_file = tmp_path / "bundle.toml"
        bundle_file.write_text(bundle_toml, encoding="utf-8")
        bundle = str(bundle_file)
    local = tmp_path / "settings.local.toml"
    local.write_text(local_toml, encoding="utf-8")
    real = Dynaconf(
        settings_files=[bundle, str(local)],
        environments=True,
        merge_enabled=True,
        env_switcher="REDFETCH_ENV",
    )
    real.ENV = env
    monkeypatch.setattr(config, "settings", real)
    monkeypatch.setattr(config, "config_dir", str(tmp_path))
    return real


def _local_file(tmp_path):
    return tmp_path / "settings.local.toml"


def _parsed(tmp_path):
    return tomllib.loads(_local_file(tmp_path).read_text(encoding="utf-8"))


def _norm(path):
    return os.path.normpath(path)


# --- list_servers --------------------------------------------------------------

def test_real_bundle_ships_known_emu_servers(tmp_path, monkeypatch):
    """The shipped bundle: known servers exist for EMU only, none configured."""
    _install_settings(tmp_path, monkeypatch)
    assert config.list_servers("LIVE") == {}
    assert config.list_servers("TEST") == {}
    servers = config.list_servers()
    assert "lazarus" in servers
    assert servers["lazarus"]["label"] == "Project Lazarus"
    assert config.is_server_configured("lazarus") is False


def test_bundled_known_entries_are_wellformed(tmp_path, monkeypatch):
    """Validate the required shape of every bundled server entry."""
    _install_settings(tmp_path, monkeypatch)
    for env in config.ENV_TOKENS:
        for slug, entry in config.list_servers(env).items():
            assert config.validate_server_slug(slug) == slug
            assert entry.get("label"), f"known server '{slug}' needs a label"
            assert len(entry["label"]) <= 19, f"'{slug}' label too long for the UI"
            assert not entry.get("opt_in"), f"'{slug}' must ship opt_in = false"
            assert not entry.get("eqpath"), f"'{slug}' must not ship an eqpath"
            shortname = entry.get("shortname")
            if shortname:
                # AutoLogin compares server names case-insensitively.
                assert shortname == shortname.strip(), f"'{slug}' shortname has stray whitespace"
            guide = entry.get("guide")
            if guide:
                assert guide.startswith("https://"), f"'{slug}' guide must be HTTPS"
            eqhost = entry.get("eqhost")
            if eqhost:
                assert re.fullmatch(r"[A-Za-z0-9.-]+:\d+", eqhost), (
                    f"'{slug}' eqhost must be host:port"
                )
            patcher_url = entry.get("patcher_url")
            if patcher_url:
                assert patcher_url.startswith("https://")
                exe = entry.get("patcher_exe")
                assert exe, f"'{slug}' patcher_url without patcher_exe"
                assert os.path.basename(exe) == exe, f"'{slug}' patcher_exe must be bare"


def test_list_servers_returns_local_entries_as_plain_dicts(tmp_path, monkeypatch):
    local = """
[EMU.SERVERS.myserver]
label = "My Server"
opt_in = true
eqpath = "D:/Games/EQ-Mine"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    servers = config.list_servers()
    assert "myserver" in servers  # bundled known entries (lazarus) ride along
    entry = servers["myserver"]
    assert type(entry) is dict  # plain dict, not a dynaconf box
    assert entry["label"] == "My Server"
    assert entry["opt_in"] is True
    assert entry["eqpath"] == "D:/Games/EQ-Mine"


def test_known_bundle_merges_with_local_deltas(tmp_path, monkeypatch):
    """The staff-pick pattern: bundled known defaults + local deltas, leaf-wise."""
    local = """
[EMU.SERVERS.thegrind]
opt_in = true
eqpath = "C:/Games/EQ-TheGrind"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local, bundle_toml=BUNDLE_WITH_KNOWN)
    entry = config.list_servers()["thegrind"]
    assert entry["label"] == "The Grind"              # from the bundle
    assert entry["opt_in"] is True                    # local delta wins
    assert entry["eqpath"] == "C:/Games/EQ-TheGrind"  # novel local leaf
    assert entry["patcher_url"] == "https://thegrind.example/patcher.zip"


def test_local_patcher_url_overrides_bundle(tmp_path, monkeypatch):
    """A vetted-URL release edit reaches users, unless they overrode it locally."""
    local = """
[EMU.SERVERS.thegrind]
patcher_url = "https://mymirror.example/patcher.zip"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local, bundle_toml=BUNDLE_WITH_KNOWN)
    entry = config.list_servers()["thegrind"]
    assert entry["patcher_url"] == "https://mymirror.example/patcher.zip"
    assert entry["patcher_exe"] == "patcher.exe"  # untouched bundle leaf survives the merge


def test_list_servers_skips_non_table_junk(tmp_path, monkeypatch):
    local = """
[EMU]
SERVERS = "oops"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    assert config.list_servers() == {}


# --- get_active_server -----------------------------------------------------------

def test_get_active_server(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml='[EMU]\nACTIVE_SERVER = "thegrind"\n')
    assert config.get_active_server() == "thegrind"


def test_get_active_server_none_when_unset(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch)
    assert config.get_active_server() is None


def test_active_server_invisible_to_other_clients(tmp_path, monkeypatch):
    """ACTIVE_SERVER is scoped to its Dynaconf environment."""
    _install_settings(tmp_path, monkeypatch, local_toml='[EMU]\nACTIVE_SERVER = "thegrind"\n')
    assert config.get_active_server("LIVE") is None
    assert config.get_active_server("TEST") is None


# --- is_server_configured ---------------------------------------------------------

def test_configured_requires_opt_in_and_eqpath(tmp_path, monkeypatch):
    local = """
[EMU.SERVERS.ready]
opt_in = true
eqpath = "D:/EQ-Ready"

[EMU.SERVERS.no_path]
opt_in = true
eqpath = ""

[EMU.SERVERS.opted_out]
opt_in = false
eqpath = "D:/EQ-Out"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    assert config.is_server_configured("ready") is True
    assert config.is_server_configured("no_path") is False
    assert config.is_server_configured("opted_out") is False
    assert config.is_server_configured("nonexistent") is False


def test_known_entry_alone_is_not_configured(tmp_path, monkeypatch):
    """A bundled server is not configured until the user enables it."""
    _install_settings(tmp_path, monkeypatch, bundle_toml=BUNDLE_WITH_KNOWN)
    assert "thegrind" in config.list_servers()
    assert config.is_server_configured("thegrind") is False


# --- slug rules --------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["thegrind", "project-quarm", "my_server2", "a"])
def test_valid_slugs_pass(slug):
    assert config.validate_server_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", None, "The Grind", "TheGrind", "grind!", "grind.zek", "grind/zek", "gr\u00ednd", "grind\n"],
)
def test_bad_slugs_rejected(slug):
    with pytest.raises(ValueError):
        config.validate_server_slug(slug)


@pytest.mark.parametrize("slug", ["live", "test", "emu"])
def test_reserved_client_tokens_rejected(slug):
    with pytest.raises(ValueError, match="reserved"):
        config.validate_server_slug(slug)


def test_must_be_new_rejects_slug_used_by_any_client(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml='[EMU.SERVERS.taken]\nlabel = "Taken"\n')
    with pytest.raises(ValueError, match="already in use"):
        config.validate_server_slug("taken", must_be_new=True)
    assert config.validate_server_slug("nottaken", must_be_new=True) == "nottaken"


def test_must_be_new_counts_known_bundle_slugs(tmp_path, monkeypatch):
    """Known slugs are taken even before a user configures them."""
    _install_settings(tmp_path, monkeypatch, bundle_toml=BUNDLE_WITH_KNOWN)
    with pytest.raises(ValueError, match="already in use"):
        config.validate_server_slug("thegrind", must_be_new=True)


# --- server switching ---------------------------------------------------------

# Two configured servers; A starts active with custom map settings.
SWITCH_LOCAL = """
[EMU]
EQPATH = "D:/EQ-A"
ACTIVE_SERVER = "a"

[EMU.SPECIAL_RESOURCES.153]
opt_in = true
custom_path = "D:/shared-maps"

[EMU.SERVERS.a]
label = "Server A"
opt_in = true
eqpath = "D:/EQ-A"

[EMU.SERVERS.b]
label = "Server B"
opt_in = true
eqpath = "D:/EQ-B"
"""


def test_switch_applies_incoming_and_saves_back_outgoing(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)

    config.switch_server("b")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "b"
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-B")
    # A's current map settings were saved to its profile.
    snap = emu["SERVERS"]["a"]
    assert _norm(snap["eqpath"]) == _norm("D:/EQ-A")
    assert snap["SPECIAL_RESOURCES"]["153"]["opt_in"] is True
    assert _norm(snap["SPECIAL_RESOURCES"]["153"]["custom_path"]) == _norm("D:/shared-maps")
    # B's missing map settings inherited defaults and were pruned.
    assert "SPECIAL_RESOURCES" not in emu


def test_switch_round_trip_is_byte_identical(tmp_path, monkeypatch):
    """A->B->A from a steady state leaves settings.local.toml byte-identical."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)

    config.switch_server("b")
    config.switch_server("a")
    baseline = _local_file(tmp_path).read_bytes()

    config.switch_server("b")
    config.switch_server("a")

    assert _local_file(tmp_path).read_bytes() == baseline
    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "a"
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-A")
    assert emu["SPECIAL_RESOURCES"]["153"]["opt_in"] is True


def test_switch_to_active_server_captures_env_edits(tmp_path, monkeypatch):
    """Re-selecting the active server snapshots current env values, not stale ones."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    config.switch_server("b")
    config.update_setting(["EQPATH"], "D:/EQ-B-moved", env="EMU")

    config.switch_server("b")

    emu = _parsed(tmp_path)["EMU"]
    assert _norm(emu["SERVERS"]["b"]["eqpath"]) == _norm("D:/EQ-B-moved")
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-B-moved")


def test_switch_unknown_slug_rejected(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    before = _local_file(tmp_path).read_bytes()
    with pytest.raises(config.ServerSwitchError, match="Unknown server"):
        config.switch_server("nope")
    assert _local_file(tmp_path).read_bytes() == before


def test_switch_unconfigured_known_rejected(tmp_path, monkeypatch):
    """Bundled servers must be configured before switching."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    with pytest.raises(config.ServerSwitchError, match="configure it first"):
        config.switch_server("lazarus")


def test_switch_blank_eqpath_rejected(tmp_path, monkeypatch):
    local = SWITCH_LOCAL + """
[EMU.SERVERS.pathless]
label = "Pathless"
opt_in = true
eqpath = ""
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    with pytest.raises(config.ServerSwitchError, match="no EverQuest folder"):
        config.switch_server("pathless")


def test_switch_blocks_blank_env_eqpath_with_opt_ins(tmp_path, monkeypatch):
    """Reject an active profile with maps enabled but no EQ path."""
    local = """
[EMU]
EQPATH = ""
ACTIVE_SERVER = "a"

[EMU.SPECIAL_RESOURCES.153]
opt_in = true

[EMU.SERVERS.a]
label = "Server A"
opt_in = true
eqpath = "D:/EQ-A"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    before = _local_file(tmp_path).read_bytes()
    with pytest.raises(config.ServerSwitchError, match="map downloads"):
        config.switch_server("a")
    assert _local_file(tmp_path).read_bytes() == before


def test_ghost_prevention_skips_save_back_to_deconfigured(tmp_path, monkeypatch):
    """A deconfigured outgoing server keeps its old snapshot verbatim."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    config.switch_server("b")
    # Change the active path, then mark B as unconfigured.
    config.update_setting(["EQPATH"], "D:/EQ-B-edited", env="EMU")
    config.update_setting(["SERVERS", "b", "opt_in"], False, env="EMU")

    config.switch_server("a")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "a"
    b_snap = emu["SERVERS"]["b"]
    assert _norm(b_snap["eqpath"]) == _norm("D:/EQ-B")  # save-back dropped
    assert b_snap["opt_in"] is False


def test_switch_cache_patch_updates_from_env_clones(tmp_path, monkeypatch):
    """Refresh cached environment views and the current EMU settings object."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL, current_env="EMU")
    clone_before = config.settings.from_env("EMU")

    config.switch_server("b")

    clone = config.settings.from_env("EMU")
    assert clone is clone_before
    assert _norm(clone.EQPATH) == _norm("D:/EQ-B")
    assert clone.get("ACTIVE_SERVER") == "b"
    servers = config.list_servers()
    assert _norm(servers["a"]["eqpath"]) == _norm("D:/EQ-A")
    assert "b" in servers and "lazarus" in servers
    assert _norm(config.settings.EQPATH) == _norm("D:/EQ-B")


def test_active_server_kept_when_snapshot_equals_defaults(tmp_path, monkeypatch):
    """ACTIVE_SERVER survives when all other values match their defaults."""
    local = """
[EMU.SERVERS.c]
label = "Server C"
opt_in = true
eqpath = "D:/EQ-C"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)

    config.switch_server("c")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "c"
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-C")
    assert "SPECIAL_RESOURCES" not in emu
    assert config.get_active_server() == "c"
