"""redfetch recovers the EQ dir from MacroQuest autologin's login.db when EQPATH is unset."""
import os
import sqlite3

import pytest

from redfetch import detecteq


def _make_login_db(db_path: str, rows: list[tuple[str, str]]) -> None:
    """A minimal login.db with just the server_types table MQ reads the EQ path from."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE server_types (type text primary key, eq_path text not null)")
        con.executemany(
            "INSERT INTO server_types (type, eq_path) VALUES (LOWER(?), ?)", rows
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def eq_dir(tmp_path):
    """A folder that passes detecteq._is_valid_eq_dir (contains eqgame.exe)."""
    d = tmp_path / "EverQuest"
    d.mkdir()
    (d / "eqgame.exe").write_text("")
    return d


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    return d


def test_reads_matching_server_type_case_insensitively(config_dir, eq_dir):
    _make_login_db(str(config_dir / "login.db"), [("live", str(eq_dir))])
    expected = os.path.normpath(str(eq_dir))
    # type stored lowercased; lookup normalizes both sides
    assert detecteq.read_autologin_eq_path(str(config_dir), "live") == expected
    assert detecteq.read_autologin_eq_path(str(config_dir), "LIVE") == expected


def test_emu_row_found_when_live_absent(config_dir, eq_dir):
    # the whole point: emu/test clients the registry can't detect
    _make_login_db(str(config_dir / "login.db"), [("emu", str(eq_dir))])
    assert detecteq.read_autologin_eq_path(str(config_dir), "emu") == os.path.normpath(str(eq_dir))
    assert detecteq.read_autologin_eq_path(str(config_dir), "live") is None


def test_missing_db_returns_none(config_dir):
    assert detecteq.read_autologin_eq_path(str(config_dir), "live") is None


def test_empty_eq_path_returns_none(config_dir):
    # the schema's 'import' placeholder row carries an empty path
    _make_login_db(str(config_dir / "login.db"), [("import", "")])
    assert detecteq.read_autologin_eq_path(str(config_dir), "import") is None


def test_stale_path_without_eqgame_returns_none(config_dir, tmp_path):
    _make_login_db(str(config_dir / "login.db"), [("live", str(tmp_path / "gone"))])
    assert detecteq.read_autologin_eq_path(str(config_dir), "live") is None


def test_old_schema_without_server_types_returns_none(config_dir):
    con = sqlite3.connect(str(config_dir / "login.db"))
    con.execute("CREATE TABLE settings (key text, value text)")
    con.commit()
    con.close()
    assert detecteq.read_autologin_eq_path(str(config_dir), "live") is None
