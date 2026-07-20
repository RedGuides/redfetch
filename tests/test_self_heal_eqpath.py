"""config.self_heal_eqpath fills a blank/stale EQPATH from MacroQuest autologin at startup."""
import os
import sqlite3

import pytest

from redfetch import config


# --- login.db / folder fixtures ---------------------------------------------

def _make_login_db(config_dir, rows):
    """A minimal login.db with just the server_types table MQ reads the EQ path from."""
    con = sqlite3.connect(str(config_dir / "login.db"))
    try:
        con.execute("CREATE TABLE server_types (type text primary key, eq_path text not null)")
        con.executemany("INSERT INTO server_types (type, eq_path) VALUES (LOWER(?), ?)", rows)
        con.commit()
    finally:
        con.close()


def _make_vvmq(tmp_path, name, server_type, eq_path):
    """A VVMQ folder whose config/login.db maps `server_type` -> `eq_path`."""
    vvmq = tmp_path / name
    (vvmq / "config").mkdir(parents=True)
    _make_login_db(vvmq / "config", [(server_type, str(eq_path))])
    return vvmq


@pytest.fixture
def eq_dir(tmp_path):
    """A valid EverQuest folder (passes detecteq._is_valid_eq_dir)."""
    d = tmp_path / "EverQuest"
    d.mkdir()
    (d / "eqgame.exe").write_text("")
    return d


# --- fake settings + update_setting spy -------------------------------------

# config.VANILLA_MAP: LIVE -> 1974, TEST -> 2218, EMU -> 60
_VVMQ_ID = {"LIVE": "1974", "TEST": "2218", "EMU": "60"}


class _FakeEnv:
    def __init__(self, eqpath=None, special_resources=None, download_folder=""):
        self._eqpath = eqpath
        self.SPECIAL_RESOURCES = special_resources or {}
        self.DOWNLOAD_FOLDER = download_folder

    def get(self, key, default=None):
        return self._eqpath if key == "EQPATH" else default


class _FakeSettings:
    def __init__(self, envs, raise_on=()):
        self._envs = envs
        self._raise_on = set(raise_on)
        self.ENV = "LIVE"

    def from_env(self, env):
        if env in self._raise_on:
            raise RuntimeError(f"boom for {env}")
        return self._envs[env]  # KeyError for unconfigured envs -> heal's except skips it


def _env_with_vvmq(env, vvmq, eqpath=None):
    """A fake env whose VVMQ special resource points at `vvmq` via custom_path."""
    return _FakeEnv(eqpath=eqpath, special_resources={_VVMQ_ID[env]: {"custom_path": str(vvmq)}})


@pytest.fixture
def spy(monkeypatch):
    """Record update_setting calls instead of touching disk; returns the call list."""
    calls = []
    monkeypatch.setattr(
        config, "update_setting",
        lambda path, value, env=None: calls.append((path, value, env)),
    )
    return calls


def _install(monkeypatch, envs, raise_on=()):
    monkeypatch.setattr(config, "settings", _FakeSettings(envs, raise_on))


def _healed(eq_dir, env):
    """The (args) tuple update_setting should receive for a heal of `env`."""
    return (["EQPATH"], os.path.normpath(str(eq_dir)), env)


# --- heal policy ------------------------------------------------------------

def test_blank_eqpath_heals_from_autologin(tmp_path, eq_dir, spy, monkeypatch):
    vvmq = _make_vvmq(tmp_path, "vvmq_live", "live", eq_dir)
    _install(monkeypatch, {"LIVE": _env_with_vvmq("LIVE", vvmq, eqpath=None)})

    config.self_heal_eqpath()

    assert spy == [_healed(eq_dir, "LIVE")]


def test_stale_eqpath_is_reheal(tmp_path, eq_dir, spy, monkeypatch):
    # stored path's folder is gone -> the empty-or-invalid middle ground re-heals it
    vvmq = _make_vvmq(tmp_path, "vvmq_live", "live", eq_dir)
    stale = str(tmp_path / "gone")
    _install(monkeypatch, {"LIVE": _env_with_vvmq("LIVE", vvmq, eqpath=stale)})

    config.self_heal_eqpath()

    assert spy == [_healed(eq_dir, "LIVE")]


def test_valid_stored_eqpath_never_clobbered(tmp_path, eq_dir, spy, monkeypatch):
    other = tmp_path / "OtherEQ"
    other.mkdir()
    (other / "eqgame.exe").write_text("")
    vvmq = _make_vvmq(tmp_path, "vvmq_live", "live", eq_dir)  # autologin knows a different valid dir
    _install(monkeypatch, {"LIVE": _env_with_vvmq("LIVE", vvmq, eqpath=str(other))})

    config.self_heal_eqpath()

    assert spy == []  # a valid deliberate value wins; no probe, no write


def test_no_login_db_no_heal(tmp_path, spy, monkeypatch):
    vvmq = tmp_path / "vvmq_live"
    (vvmq / "config").mkdir(parents=True)  # MQ folder exists but autologin never ran
    _install(monkeypatch, {"LIVE": _env_with_vvmq("LIVE", vvmq, eqpath=None)})

    config.self_heal_eqpath()

    assert spy == []


def test_exception_in_one_env_does_not_abort_others(tmp_path, eq_dir, spy, monkeypatch):
    vvmq_test = _make_vvmq(tmp_path, "vvmq_test", "test", eq_dir)
    # LIVE blows up on access; the later TEST env must still heal (isolation)
    _install(
        monkeypatch,
        {"TEST": _env_with_vvmq("TEST", vvmq_test, eqpath=None)},
        raise_on={"LIVE"},
    )

    config.self_heal_eqpath()

    assert spy == [_healed(eq_dir, "TEST")]


# --- the @format linchpin ---------------------------------------------------

def test_maps_resolve_under_stored_eqpath(tmp_path, monkeypatch):
    """A stored EQPATH (where the heal writes it) makes maps resource 153 resolve to <eq>/maps."""
    from dynaconf import Dynaconf
    from redfetch import sync_discovery

    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    eq = tmp_path / "EverQuest"
    eq.mkdir()

    # The heal lands EQPATH in settings.local.toml; write it there and read it back like production.
    local = tmp_path / "settings.local.toml"
    local.write_text(f'[LIVE]\nEQPATH = "{eq.as_posix()}"\n', encoding="utf-8")

    real = Dynaconf(
        settings_files=[os.path.join(config.script_dir, "settings.toml"), str(local)],
        environments=True,
        merge_enabled=True,
        env_switcher="REDFETCH_ENV",
    )
    real.ENV = "LIVE"
    monkeypatch.setattr(config, "settings", real)

    resolved = sync_discovery.resolve_root_path("153", None, "LIVE")
    assert resolved == os.path.normpath(str(eq / "maps"))

