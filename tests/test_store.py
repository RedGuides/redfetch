"""Tests for database connection lifecycle."""

import os
import sqlite3

import pytest

from redfetch import config, store

DB_NAME = "test_resources.db"


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REDFETCH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "config_dir", None)


def test_connection_closes_on_exit(store_env):
    with store.get_db_connection(DB_NAME) as conn:
        conn.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connection_closes_on_exception(store_env):
    with pytest.raises(ValueError):
        with store.get_db_connection(DB_NAME) as conn:
            raise ValueError("boom")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_writes_persist_without_explicit_commit(store_env):
    with store.get_db_connection(DB_NAME) as conn:
        store.initialize_schema(conn.cursor())
        conn.execute(
            """
            INSERT INTO downloads (target_key, resource_id, root_resource_id, target_kind)
            VALUES ('/42/', 42, 42, 'root')
            """
        )

    with store.get_db_connection(DB_NAME) as conn:
        row = conn.execute("SELECT resource_id FROM downloads WHERE target_key = '/42/'").fetchone()
    assert row == (42,)


def test_initialize_db_creates_schema(store_env):
    store.initialize_db(DB_NAME)

    with store.get_db_connection(DB_NAME) as conn:
        row = conn.execute("SELECT schema_version FROM metadata WHERE id = 1").fetchone()
    assert row == (store.SCHEMA_VERSION,)


_V2_DDL = """
CREATE TABLE downloads (
    id INTEGER PRIMARY KEY,
    target_key TEXT UNIQUE,
    resource_id INTEGER NOT NULL,
    parent_id INTEGER NOT NULL DEFAULT 0,
    parent_target_key TEXT,
    root_resource_id INTEGER NOT NULL DEFAULT 0,
    target_kind TEXT NOT NULL DEFAULT 'root',
    category_id INTEGER,
    title TEXT,
    version_remote INTEGER,
    version_local INTEGER,
    resolved_path TEXT,
    subfolder TEXT,
    flatten INTEGER NOT NULL DEFAULT 0,
    protected_files TEXT NOT NULL DEFAULT '[]',
    remote_status TEXT,
    is_special INTEGER NOT NULL DEFAULT 0,
    is_watching INTEGER NOT NULL DEFAULT 0,
    is_licensed INTEGER NOT NULL DEFAULT 0,
    is_explicit INTEGER NOT NULL DEFAULT 0,
    is_dependency INTEGER NOT NULL DEFAULT 0
)
"""


def test_v2_schema_migrates_to_v3_preserving_rows(store_env):
    """The v2 -> v3 migration must keep install state (no mass re-download)."""
    with store.get_db_connection(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, schema_version INTEGER)")
        cursor.execute("INSERT INTO metadata (id, schema_version) VALUES (1, 2)")
        cursor.execute(_V2_DDL)
        cursor.execute(
            "CREATE INDEX idx_downloads_resource_id ON downloads(resource_id)"
        )
        cursor.execute(
            """
            INSERT INTO downloads (target_key, resource_id, root_resource_id, target_kind,
                                   version_local, resolved_path)
            VALUES ('/153/', 153, 153, 'root', 9, 'C:/eq/maps')
            """
        )
        cursor.execute(
            """
            INSERT INTO downloads (target_key, resource_id, parent_id, parent_target_key,
                                   root_resource_id, target_kind, version_local)
            VALUES ('/151/1865/', 1865, 151, '/151/', 151, 'dependency', 7)
            """
        )

    store.initialize_db(DB_NAME)

    with store.get_db_connection(DB_NAME) as conn:
        cursor = conn.cursor()
        assert cursor.execute("SELECT schema_version FROM metadata WHERE id = 1").fetchone() == (
            store.SCHEMA_VERSION,
        )
        rows = cursor.execute(
            "SELECT target_key, server_slug, version_local, resolved_path FROM downloads ORDER BY target_key"
        ).fetchall()
        assert rows == [
            ("/151/1865/", "", 7, None),
            ("/153/", "", 9, "C:/eq/maps"),
        ]
        # The old table (and its indexes) are gone; fresh indexes cover the new table.
        assert cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='downloads_v2'"
        ).fetchone() == (0,)
        index_tables = cursor.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='idx_downloads_resource_id'"
        ).fetchall()
        assert index_tables == [("downloads",)]


def test_same_target_key_allowed_per_server_slug(store_env):
    """(target_key, server_slug) is the row identity; a bare duplicate still collides."""
    store.initialize_db(DB_NAME)
    with store.get_db_connection(DB_NAME) as conn:
        base = "INSERT INTO downloads (target_key, server_slug, resource_id, root_resource_id, target_kind) VALUES (?, ?, 153, 153, 'root')"
        conn.execute(base, ("/153/", "servera"))
        conn.execute(base, ("/153/", "serverb"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, ("/153/", "servera"))


def test_reset_versions_scoped_to_active_server(store_env):
    """A forced reset touches env-level + active-server rows, never other servers'."""
    store.initialize_db(DB_NAME)
    with store.get_db_connection(DB_NAME) as conn:
        base = ("INSERT INTO downloads (target_key, server_slug, resource_id, root_resource_id,"
                " target_kind, version_local) VALUES ('/153/', ?, 153, 153, 'root', 9)")
        for slug in ("", "servera", "serverb"):
            conn.execute(base, (slug,))
        store.reset_versions_for_resource(conn.cursor(), "153", "servera")
        rows = conn.execute(
            "SELECT server_slug, version_local FROM downloads ORDER BY server_slug"
        ).fetchall()
    assert rows == [("", 0), ("servera", 0), ("serverb", 9)]


def test_db_file_deletable_after_use(store_env):
    """Closing the connection must release the database file on Windows."""
    store.initialize_db(DB_NAME)
    with store.get_db_connection(DB_NAME) as conn:
        store.initialize_schema(conn.cursor())

    db_path = store.get_db_path(DB_NAME)
    os.remove(db_path)
    for sidecar in (f"{db_path}-wal", f"{db_path}-shm"):
        if os.path.exists(sidecar):
            os.remove(sidecar)
