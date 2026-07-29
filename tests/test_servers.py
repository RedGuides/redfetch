"""SERVERS / ACTIVE_SERVER accessors and slug rules (emu multipath, Phase 1).

Read helpers only in this commit: nothing at runtime consumes these keys yet.
The switch/lifecycle writers grow into this file in later commits.
"""
import os
import re

import pytest
from dynaconf import Dynaconf

from redfetch import config


# --- fixtures ----------------------------------------------------------------

# Fixture standing in for known entries, so merge tests don't depend on which
# real entries ship in settings.toml; shape matches the emu-multipath-plan.md data model.
BUNDLE_WITH_KNOWN = """
[EMU.SERVERS.thegrind]
label = "The Grind"
opt_in = false
patcher_url = "https://thegrind.example/patcher.zip"
patcher_exe = "patcher.exe"
"""


def _install_settings(tmp_path, monkeypatch, local_toml="", bundle_toml=None, env="EMU"):
    """Real Dynaconf over (bundle, local) files, installed as config.settings.

    bundle_toml=None uses the real bundled settings.toml; a string uses a fixture
    bundle instead (the known-servers case).
    """
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
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
    return real


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
    """Real-data smoke: every known entry in the shipped bundle validates.

    Pins the bundle's contract: valid slug, short label, ships unconfigured, and
    (when present) a verbatim login-list shortname, an HTTPS getting-started
    guide, a host:port eqhost, and an HTTPS patcher URL with a bare-filename exe.
    """
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
                # verbatim login-list value; autologin matching lowercases both sides
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
    """environments=True scopes ACTIVE_SERVER under [EMU]; LIVE/TEST structurally can't see it."""
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
    """Bundled existence isn't configuration — the ghost-prevention predicate."""
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
