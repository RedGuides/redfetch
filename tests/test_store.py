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
