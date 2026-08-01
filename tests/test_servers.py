"""Tests for emulator server profiles and switching."""
import os
import re
import tomllib

import pytest
from dynaconf import Dynaconf

from redfetch import config, servers, store


# --- fixtures ----------------------------------------------------------------

# Stable bundled-server fixture for merge tests.
BUNDLE_WITH_KNOWN = """
[EMU.SERVERS.thegrind]
label = "The Grind"
opt_in = false
patcher_url = "https://thegrind.example/patcher.zip"
patcher_exe = "patcher.exe"
"""

# A second multi-server client (paired with monkeypatched MULTI_SERVER_ENVS).
BUNDLE_TWO_CLIENTS = BUNDLE_WITH_KNOWN + """
[TOB.SERVERS.veeshan]
label = "Veeshan"
opt_in = false
"""


def _install_settings(tmp_path, monkeypatch, local_toml="", bundle_toml=None, env="EMU",
                      current_env=None):
    """Install a real Dynaconf instance backed by temporary settings files.

    A fixture bundle also swaps config._base_settings_cache, so mutator tests
    prune/validate against the fixture rather than the real bundle.
    """
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    if current_env:
        monkeypatch.setenv("REDFETCH_ENV", current_env)
    if bundle_toml is None:
        bundle = os.path.join(config.script_dir, "settings.toml")
    else:
        bundle_file = tmp_path / "bundle.toml"
        bundle_file.write_text(bundle_toml, encoding="utf-8")
        bundle = str(bundle_file)
        fixture_base = Dynaconf(
            settings_files=[bundle],
            environments=True,
            merge_enabled=True,
            env_switcher="REDFETCH_ENV",
        )
        monkeypatch.setattr(config, "_base_settings_cache", fixture_base)
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
    assert servers.list_servers("LIVE") == {}
    assert servers.list_servers("TEST") == {}
    listed = servers.list_servers("EMU")
    assert "lazarus" in listed
    assert listed["lazarus"]["label"] == "Project Lazarus"
    assert servers.is_server_configured("lazarus", "EMU") is False


def test_bundled_known_entries_are_wellformed(tmp_path, monkeypatch):
    """Validate the required shape of every bundled server entry."""
    _install_settings(tmp_path, monkeypatch)
    for env in config.ENV_TOKENS:
        for slug, entry in servers.list_servers(env).items():
            assert servers.validate_server_slug(slug) == slug
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
    listed = servers.list_servers("EMU")
    assert "myserver" in listed  # bundled known entries (lazarus) ride along
    entry = listed["myserver"]
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
    entry = servers.list_servers("EMU")["thegrind"]
    assert entry["label"] == "The Grind"              # from the bundle
    assert entry["opt_in"] is True                    # local delta wins
    assert entry["eqpath"] == "C:/Games/EQ-TheGrind"  # novel local leaf
    assert entry["patcher_url"] == "https://thegrind.example/patcher.zip"


def test_list_servers_skips_non_table_junk(tmp_path, monkeypatch):
    local = """
[EMU]
SERVERS = "oops"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    assert servers.list_servers("EMU") == {}


# --- get_active_server -----------------------------------------------------------

def test_get_active_server_scoped_to_client(tmp_path, monkeypatch):
    """ACTIVE_SERVER is scoped to its Dynaconf environment; unset reads as None."""
    _install_settings(tmp_path, monkeypatch, local_toml='[EMU]\nACTIVE_SERVER = "thegrind"\n')
    assert servers.get_active_server("EMU") == "thegrind"
    assert servers.get_active_server("LIVE") is None
    assert servers.get_active_server("TEST") is None


def test_active_server_slug_for_db_keying(tmp_path, monkeypatch):
    """'' on single-server envs and when no server is active — never None."""
    _install_settings(tmp_path, monkeypatch, local_toml='[EMU]\nACTIVE_SERVER = "thegrind"\n')
    assert servers.active_server_slug("EMU") == "thegrind"
    assert servers.active_server_slug("LIVE") == ""
    assert servers.active_server_slug("TEST") == ""


def test_active_server_slug_empty_when_unset(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch)
    assert servers.active_server_slug("EMU") == ""


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
    assert servers.is_server_configured("ready", "EMU") is True
    assert servers.is_server_configured("no_path", "EMU") is False
    assert servers.is_server_configured("opted_out", "EMU") is False
    assert servers.is_server_configured("nonexistent", "EMU") is False


# --- is_known_server ----------------------------------------------------------

def test_is_known_server_distinguishes_bundle_from_local(tmp_path, monkeypatch):
    local = '[EMU.SERVERS.myserver]\nlabel = "My Server"\n'
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    assert servers.is_known_server("lazarus", "EMU") is True
    assert servers.is_known_server("myserver", "EMU") is False
    assert servers.is_known_server("nonexistent", "EMU") is False


# --- multi-server seam ---------------------------------------------------------

def test_is_multi_server_predicate(monkeypatch):
    assert servers.is_multi_server("EMU") is True
    assert servers.is_multi_server("LIVE") is False
    assert servers.is_multi_server("TEST") is False
    # Uniqueness sweeps and the heal loop iterate ENV_TOKENS; a multi-server
    # env missing from it would silently escape both.
    assert set(config.MULTI_SERVER_ENVS) <= set(config.ENV_TOKENS)
    monkeypatch.setattr(config, "MULTI_SERVER_ENVS", ("EMU", "TOB"))
    assert servers.is_multi_server("TOB") is True


def test_env_for_slug_unique_resolution(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, bundle_toml=BUNDLE_TWO_CLIENTS)
    monkeypatch.setattr(config, "MULTI_SERVER_ENVS", ("EMU", "TOB"))
    assert servers.env_for_slug("thegrind") == "EMU"
    assert servers.env_for_slug("veeshan") == "TOB"
    assert servers.env_for_slug("nope") is None


def test_env_for_slug_ambiguous_raises(tmp_path, monkeypatch):
    """The same slug under two envs: hand-edits bypass add-path uniqueness."""
    local = '[TOB.SERVERS.thegrind]\nlabel = "Impostor"\n'
    _install_settings(tmp_path, monkeypatch, local_toml=local, bundle_toml=BUNDLE_TWO_CLIENTS)
    monkeypatch.setattr(config, "MULTI_SERVER_ENVS", ("EMU", "TOB"))
    with pytest.raises(ValueError, match="ambiguous"):
        servers.env_for_slug("thegrind")


def test_switch_server_derives_env_from_slug(tmp_path, monkeypatch):
    """switch_server writes to the slug's own env, not a hardcoded one."""
    local = """
[TOB.SERVERS.veeshan]
opt_in = true
eqpath = "D:/EQ-Veeshan"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local, bundle_toml=BUNDLE_TWO_CLIENTS)
    monkeypatch.setattr(config, "MULTI_SERVER_ENVS", ("EMU", "TOB"))

    servers.switch_server("veeshan")

    parsed = _parsed(tmp_path)
    assert parsed["TOB"]["ACTIVE_SERVER"] == "veeshan"
    assert _norm(parsed["TOB"]["EQPATH"]) == _norm("D:/EQ-Veeshan")
    assert "EMU" not in parsed  # the other multi-server env untouched
    assert servers.get_active_server("TOB") == "veeshan"
    assert servers.get_active_server("EMU") is None


# --- slug rules --------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["thegrind", "project-quarm", "my_server2", "a"])
def test_valid_slugs_pass(slug):
    assert servers.validate_server_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", None, "The Grind", "TheGrind", "grind!", "grind.zek", "grind/zek", "gr\u00ednd", "grind\n"],
)
def test_bad_slugs_rejected(slug):
    with pytest.raises(ValueError):
        servers.validate_server_slug(slug)


@pytest.mark.parametrize("slug", ["live", "test", "emu"])
def test_reserved_client_tokens_rejected(slug):
    with pytest.raises(ValueError, match="reserved"):
        servers.validate_server_slug(slug)


def test_must_be_new_rejects_slug_used_by_any_client(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml='[EMU.SERVERS.taken]\nlabel = "Taken"\n')
    with pytest.raises(ValueError, match="already in use"):
        servers.validate_server_slug("taken", must_be_new=True)
    assert servers.validate_server_slug("nottaken", must_be_new=True) == "nottaken"


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

    servers.switch_server("b")

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

    servers.switch_server("b")
    servers.switch_server("a")
    baseline = _local_file(tmp_path).read_bytes()

    servers.switch_server("b")
    servers.switch_server("a")

    assert _local_file(tmp_path).read_bytes() == baseline
    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "a"
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-A")
    assert emu["SPECIAL_RESOURCES"]["153"]["opt_in"] is True


def test_switch_to_active_server_captures_env_edits(tmp_path, monkeypatch):
    """Re-selecting the active server snapshots current env values, not stale ones."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    servers.switch_server("b")
    config.update_setting(["EQPATH"], "D:/EQ-B-moved", env="EMU")

    servers.switch_server("b")

    emu = _parsed(tmp_path)["EMU"]
    assert _norm(emu["SERVERS"]["b"]["eqpath"]) == _norm("D:/EQ-B-moved")
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-B-moved")


def test_switch_unknown_slug_rejected(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    before = _local_file(tmp_path).read_bytes()
    with pytest.raises(servers.ServerSwitchError, match="Unknown server"):
        servers.switch_server("nope")
    assert _local_file(tmp_path).read_bytes() == before


def test_switch_opted_out_rejected(tmp_path, monkeypatch):
    """A folder alone isn't enough: opt_in = false still blocks the switch."""
    local = SWITCH_LOCAL + """
[EMU.SERVERS.lazarus]
eqpath = "D:/EQ-Laz"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    with pytest.raises(servers.ServerSwitchError, match="isn't set up"):
        servers.switch_server("lazarus")


def test_switch_blank_eqpath_rejected(tmp_path, monkeypatch):
    local = SWITCH_LOCAL + """
[EMU.SERVERS.pathless]
label = "Pathless"
opt_in = true
eqpath = ""
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    with pytest.raises(servers.ServerSwitchError, match="no EverQuest folder"):
        servers.switch_server("pathless")


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
    with pytest.raises(servers.ServerSwitchError, match="map downloads"):
        servers.switch_server("a")
    assert _local_file(tmp_path).read_bytes() == before


def test_ghost_prevention_skips_save_back_to_deconfigured(tmp_path, monkeypatch):
    """A deconfigured outgoing server keeps its old snapshot verbatim."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    servers.switch_server("b")
    # Change the active path, then mark B as unconfigured.
    config.update_setting(["EQPATH"], "D:/EQ-B-edited", env="EMU")
    config.update_setting(["SERVERS", "b", "opt_in"], False, env="EMU")

    notices = servers.switch_server("a")

    assert any("won't be saved back" in notice for notice in notices)
    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "a"
    b_snap = emu["SERVERS"]["b"]
    assert _norm(b_snap["eqpath"]) == _norm("D:/EQ-B")  # save-back dropped
    assert b_snap["opt_in"] is False


def test_switch_refreshes_from_env_views(tmp_path, monkeypatch):
    """Switch invalidates cached env views; subsequent reads are fresh."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL, current_env="EMU")
    clone_before = config.settings.from_env("EMU")

    servers.switch_server("b")

    clone = config.settings.from_env("EMU")
    assert clone is not clone_before  # stale clone dropped, not patched
    assert _norm(clone.EQPATH) == _norm("D:/EQ-B")
    assert clone.get("ACTIVE_SERVER") == "b"
    listed = servers.list_servers("EMU")
    assert _norm(listed["a"]["eqpath"]) == _norm("D:/EQ-A")
    assert "b" in listed and "lazarus" in listed
    assert _norm(config.settings.EQPATH) == _norm("D:/EQ-B")


def test_dynaconf_clone_cache_semantics(tmp_path):
    """Tripwire: reload() leaves from_env() clones stale; clearing env_cache
    restores freshness. Fails loudly if dynaconf changes either behavior."""
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text('[emu]\nvalue = "old"\n', encoding="utf-8")
    s = Dynaconf(settings_files=[str(settings_file)], environments=True, merge_enabled=True)
    assert s.from_env("EMU").VALUE == "old"

    settings_file.write_text('[emu]\nvalue = "new"\n', encoding="utf-8")
    s.reload()
    assert s.from_env("EMU").VALUE == "old"  # clones survive reload()

    s.__core__.config.env_cache.clear()
    assert s.from_env("EMU").VALUE == "new"  # cleared cache rebuilds fresh


def test_active_server_kept_when_snapshot_equals_defaults(tmp_path, monkeypatch):
    """ACTIVE_SERVER survives when all other values match their defaults."""
    local = """
[EMU.SERVERS.c]
label = "Server C"
opt_in = true
eqpath = "D:/EQ-C"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)

    servers.switch_server("c")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "c"
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-C")
    assert "SPECIAL_RESOURCES" not in emu
    assert servers.get_active_server("EMU") == "c"


# --- lifecycle: add / delete / rename -------------------------------------------

def test_first_add_seeds_snapshot_and_activates(tmp_path, monkeypatch):
    """Today's setup becomes server #1: seeded from env slots, active, no switch."""
    local = """
[EMU]
EQPATH = "D:/EQ-Original"

[EMU.SPECIAL_RESOURCES.153]
opt_in = true
custom_path = "D:/shared-maps"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)

    servers.add_server("lazarus", env="EMU", eqpath="D:/EQ-Original")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "lazarus"
    snap = emu["SERVERS"]["lazarus"]
    assert snap["opt_in"] is True
    assert _norm(snap["eqpath"]) == _norm("D:/EQ-Original")
    assert snap["SPECIAL_RESOURCES"]["153"]["opt_in"] is True  # seeded from env
    assert _norm(snap["SPECIAL_RESOURCES"]["153"]["custom_path"]) == _norm("D:/shared-maps")
    assert servers.get_active_server("EMU") == "lazarus"  # fresh view
    assert servers.is_server_configured("lazarus", "EMU") is True


def test_second_add_does_not_switch(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)

    servers.add_server("myquarm", env="EMU", eqpath="D:/EQ-Quarm", label="My Quarm")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "a"  # unchanged
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-A")  # env untouched
    snap = emu["SERVERS"]["myquarm"]
    assert snap["label"] == "My Quarm"
    assert snap["opt_in"] is True
    assert "SPECIAL_RESOURCES" not in snap  # later adds don't seed
    assert servers.get_active_server("EMU") == "a"
    assert servers.is_server_configured("myquarm", "EMU") is True  # fresh view


def test_add_on_active_slug_writes_through_env_eqpath(tmp_path, monkeypatch):
    """Re-adding the active server updates both snapshot and env slot."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    config.update_setting(["SERVERS", "a", "opt_in"], False, env="EMU")

    servers.add_server("a", env="EMU", eqpath="D:/EQ-A-New")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "a"
    assert _norm(emu["EQPATH"]) == _norm("D:/EQ-A-New")
    assert _norm(emu["SERVERS"]["a"]["eqpath"]) == _norm("D:/EQ-A-New")
    # Switch-away saves the new folder, not a stale env slot.
    servers.switch_server("b")
    a_snap = _parsed(tmp_path)["EMU"]["SERVERS"]["a"]
    assert _norm(a_snap["eqpath"]) == _norm("D:/EQ-A-New")


def test_add_known_leaves_label_to_bundle(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)

    servers.add_server("lazarus", env="EMU", eqpath="D:/EQ-Laz", label="Ignored")

    snap = _parsed(tmp_path)["EMU"]["SERVERS"]["lazarus"]
    assert "label" not in snap  # bundle owns known labels
    assert servers.list_servers("EMU")["lazarus"]["label"] == "Project Lazarus"
    assert servers.is_server_configured("lazarus", "EMU") is True


def test_add_requires_folder(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="folder"):
        servers.add_server("nofolder", env="EMU", eqpath="   ")


def test_add_rejects_single_server_env(tmp_path, monkeypatch):
    """The server domain only exists for multi-server clients."""
    _install_settings(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="switchable servers"):
        servers.add_server("stray", env="LIVE", eqpath="D:/EQ-X")


def test_add_novel_rejects_cross_client_collision(tmp_path, monkeypatch):
    local = '[LIVE.SERVERS.claimed]\nlabel = "Claimed"\n'
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    with pytest.raises(ValueError, match="already in use"):
        servers.add_server("claimed", env="EMU", eqpath="D:/EQ-X")


def test_delete_active_clears_active_server(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)

    servers.delete_server("a", env="EMU")

    emu = _parsed(tmp_path)["EMU"]
    assert "ACTIVE_SERVER" not in emu
    assert "a" not in emu["SERVERS"]
    assert servers.get_active_server("EMU") is None  # fresh view
    listed = servers.list_servers("EMU")
    assert "a" not in listed and "b" in listed


def test_delete_known_reverts_to_available(tmp_path, monkeypatch):
    local = SWITCH_LOCAL + """
[EMU.SERVERS.lazarus]
opt_in = true
eqpath = "D:/EQ-Laz"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    assert servers.is_server_configured("lazarus", "EMU") is True

    servers.delete_server("lazarus", env="EMU")

    assert "lazarus" not in _parsed(tmp_path)["EMU"]["SERVERS"]  # local leaves gone
    listed = servers.list_servers("EMU")  # fresh view shows the bundled entry
    assert listed["lazarus"]["label"] == "Project Lazarus"
    assert servers.is_server_configured("lazarus", "EMU") is False


def test_delete_unknown_rejected(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Unknown server"):
        servers.delete_server("nope", env="EMU")


def test_rename_round_trip_preserves_bytes(tmp_path, monkeypatch):
    """Steady-state rename cycle: contents and bytes survive re-keying."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    servers.switch_server("b")  # "a" gets a full snapshot via save-back

    servers.rename_server("a", "atemp", env="EMU")
    servers.rename_server("atemp", "a", env="EMU")
    baseline = _local_file(tmp_path).read_bytes()

    servers.rename_server("a", "atemp", env="EMU")
    servers.rename_server("atemp", "a", env="EMU")

    assert _local_file(tmp_path).read_bytes() == baseline
    snap = _parsed(tmp_path)["EMU"]["SERVERS"]["a"]
    assert _norm(snap["eqpath"]) == _norm("D:/EQ-A")
    assert snap["SPECIAL_RESOURCES"]["153"]["opt_in"] is True


def test_rename_active_retargets(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)

    servers.rename_server("a", "alpha", env="EMU")

    emu = _parsed(tmp_path)["EMU"]
    assert emu["ACTIVE_SERVER"] == "alpha"
    assert "a" not in emu["SERVERS"] and "alpha" in emu["SERVERS"]
    assert servers.get_active_server("EMU") == "alpha"  # fresh view
    listed = servers.list_servers("EMU")
    assert "a" not in listed
    assert _norm(listed["alpha"]["eqpath"]) == _norm("D:/EQ-A")


# --- db row hygiene (C12) ---------------------------------------------------------

def _seed_maps_rows(env, slugs):
    db_name = f"{env}_resources.db"
    store.initialize_db(db_name)
    with store.get_db_connection(db_name) as conn:
        for slug in slugs:
            conn.execute(
                "INSERT INTO downloads (target_key, server_slug, resource_id, root_resource_id,"
                " target_kind, version_local) VALUES ('/153/', ?, 153, 153, 'root', 7)",
                (slug,),
            )


def _maps_row_slugs(env):
    with store.get_db_connection(f"{env}_resources.db") as conn:
        return [row[0] for row in conn.execute(
            "SELECT server_slug FROM downloads WHERE target_key = '/153/' ORDER BY server_slug"
        )]


def test_rename_rekeys_db_rows(tmp_path, monkeypatch):
    """A renamed server keeps its per-server rows; orphans under the new name go."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    _seed_maps_rows("EMU", ["", "a", "alpha"])  # 'alpha' = orphan from a pre-fix delete

    servers.rename_server("a", "alpha", env="EMU")

    assert _maps_row_slugs("EMU") == ["", "alpha"]


def test_delete_purges_db_rows(tmp_path, monkeypatch):
    """A deleted server's rows go with it; env-level rows stay."""
    _install_settings(tmp_path, monkeypatch, local_toml=SWITCH_LOCAL)
    _seed_maps_rows("EMU", ["", "a"])

    servers.delete_server("a", env="EMU")

    assert _maps_row_slugs("EMU") == [""]


def test_rename_rejects_known_and_collisions(tmp_path, monkeypatch):
    local = SWITCH_LOCAL + """
[LIVE.SERVERS.claimed]
label = "Claimed"
"""
    _install_settings(tmp_path, monkeypatch, local_toml=local)
    with pytest.raises(ValueError, match="known server"):
        servers.rename_server("lazarus", "laz2", env="EMU")
    with pytest.raises(ValueError, match="already in use"):
        servers.rename_server("a", "lazarus", env="EMU")  # bundle slug collision (unconfigured counts)
    with pytest.raises(ValueError, match="already in use"):
        servers.rename_server("a", "claimed", env="EMU")  # cross-client collision
