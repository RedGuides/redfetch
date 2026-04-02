from __future__ import annotations

from collections.abc import Iterable
import json
import os
import sqlite3

import aiosqlite

from redfetch import config
from redfetch import meta
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    ExecutionPlan,
    ExecutionResult,
    LocalInstallState,
    LocalSnapshot,
    PlannedAction,
    RemoteResourceState,
    RemoteSnapshot,
)


SCHEMA_VERSION = 2


def _get_cache_dir() -> str:
    base = getattr(config, "config_dir", None) or os.getenv("REDFETCH_CONFIG_DIR")
    if not base:
        base = os.getcwd()
    cache_dir = os.path.join(base, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_db_path(db_name: str) -> str:
    return os.path.join(_get_cache_dir(), db_name)


def get_db_connection(db_name: str):
    db_path = get_db_path(db_name)
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def initialize_db(db_name: str):
    db_path = get_db_path(db_name)
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        initialize_schema(cursor)
        conn.commit()


def _ensure_metadata(cursor) -> None:
    try:
        cursor.execute("SELECT schema_version FROM metadata WHERE id = 1")
        row = cursor.fetchone()
        if row and row[0] is not None and int(row[0]) >= SCHEMA_VERSION:
            return
    except Exception:
        pass
    _reset_sync_schema(cursor)


def _ensure_downloads_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
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
    )


def _ensure_indexes(cursor) -> None:
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_resource_id ON downloads(resource_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_root_resource_id ON downloads(root_resource_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_parent_target_key ON downloads(parent_target_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_parent_id ON downloads(parent_id)"
    )


def _ensure_navmesh_tables(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS navmesh_files (
            filename TEXT PRIMARY KEY,
            md5_hash TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS navmesh_cache (
            env TEXT PRIMARY KEY,
            etag TEXT,
            last_modified TEXT,
            manifest_json TEXT
        )
        """
    )


def _reset_sync_schema(cursor) -> None:
    cursor.execute("DROP TABLE IF EXISTS downloads")
    cursor.execute("DROP TABLE IF EXISTS resources")
    cursor.execute("DROP TABLE IF EXISTS dependencies")
    cursor.execute("DROP TABLE IF EXISTS metadata")
    cursor.execute(
        """
        CREATE TABLE metadata (
            id INTEGER PRIMARY KEY,
            schema_version INTEGER
        )
        """
    )
    cursor.execute(
        "INSERT INTO metadata (id, schema_version) VALUES (1, ?)",
        (SCHEMA_VERSION,),
    )


def initialize_schema(cursor) -> None:
    _ensure_metadata(cursor)
    _ensure_downloads_table(cursor)
    _ensure_indexes(cursor)
    _ensure_navmesh_tables(cursor)


def reset_all_versions(cursor) -> None:
    cursor.execute("UPDATE downloads SET version_local = 0")


def reset_versions_for_resource(cursor, resource_id: str) -> None:
    key = f"/{resource_id}/"
    cursor.execute(
        """
        UPDATE downloads
        SET version_local = 0
        WHERE target_key = ?
           OR target_key LIKE ?
        """,
        (
            key,
            f"{key}%",
        ),
    )


def _decode_protected_files(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _row_to_local_state(row: sqlite3.Row | tuple) -> LocalInstallState:
    (
        target_key,
        resource_id,
        parent_id,
        parent_target_key,
        root_resource_id,
        target_kind,
        category_id,
        title,
        version_remote,
        version_local,
        resolved_path,
        subfolder,
        flatten,
        protected_files,
        _remote_status,
        is_special,
        is_watching,
        is_licensed,
        is_explicit,
        is_dependency,
    ) = row

    return LocalInstallState(
        target_key=str(target_key),
        resource_id=str(resource_id),
        parent_id=str(parent_id) if parent_id not in (None, 0) else None,
        parent_target_key=parent_target_key,
        root_resource_id=str(root_resource_id),
        target_kind=str(target_kind),
        category_id=category_id,
        title=title,
        version_local=version_local,
        version_remote=version_remote,
        resolved_path=resolved_path,
        subfolder=subfolder,
        flatten=bool(flatten),
        protected_files=_decode_protected_files(protected_files),
        is_special=bool(is_special),
        is_watching=bool(is_watching),
        is_licensed=bool(is_licensed),
        is_explicit=bool(is_explicit),
        is_dependency=bool(is_dependency),
    )


async def load_local_snapshot(db_path: str) -> LocalSnapshot:
    """Load all tracked install targets from the DB so the planner knows what's installed."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        async with conn.execute(
            """
            SELECT
                target_key, resource_id, parent_id, parent_target_key, root_resource_id,
                target_kind, category_id, title, version_remote, version_local,
                resolved_path, subfolder, flatten,
                protected_files, remote_status,
                is_special, is_watching, is_licensed, is_explicit, is_dependency
            FROM downloads
            """
        ) as cursor:
            rows = await cursor.fetchall()

    return LocalSnapshot(
        install_targets={
            str(row[0]): _row_to_local_state(row)
            for row in rows
        }
    )


def _desired_flags(target: DesiredInstallTarget) -> dict[str, int]:
    """Convert a target's source set into integer flags for the DB."""
    return {
        "is_special": int("special" in target.sources),
        "is_watching": int("watching" in target.sources),
        "is_licensed": int("licensed" in target.sources),
        "is_explicit": int(target.explicit_root or "explicit" in target.sources),
        "is_dependency": int(target.target_kind == "dependency"),
    }


async def _upsert_download_row(
    conn: aiosqlite.Connection,
    *,
    target: DesiredInstallTarget,
    action: PlannedAction | None,
    remote_state: RemoteResourceState | None,
    version_local: int | None,
) -> None:
    """Save or overwrite a single download row with the best-known values from each source."""
    flags = _desired_flags(target)
    persisted_category_id = (
        action.category_id
        if action and action.category_id is not None
        else remote_state.category_id if remote_state and remote_state.category_id is not None
        else target.category_id
    )
    persisted_title = (
        action.title
        if action and action.title is not None
        else remote_state.title if remote_state and remote_state.title is not None
        else target.title
    )
    persisted_resolved_path = (
        action.resolved_path
        if action and action.resolved_path is not None
        else target.resolved_path
    )
    persisted_subfolder = (
        action.subfolder
        if action and action.subfolder is not None
        else target.subfolder
    )
    persisted_flatten = action.flatten if action is not None else target.flatten
    persisted_protected_files = action.protected_files if action is not None else target.protected_files
    await conn.execute(
        """
        INSERT INTO downloads (
            target_key, resource_id, parent_id, parent_target_key, root_resource_id,
            target_kind, category_id, title, version_remote, version_local,
            resolved_path, subfolder, flatten,
            protected_files, remote_status,
            is_special, is_watching, is_licensed, is_explicit, is_dependency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_key) DO UPDATE SET
            resource_id = excluded.resource_id,
            parent_id = excluded.parent_id,
            parent_target_key = excluded.parent_target_key,
            root_resource_id = excluded.root_resource_id,
            target_kind = excluded.target_kind,
            category_id = excluded.category_id,
            title = excluded.title,
            version_remote = excluded.version_remote,
            version_local = excluded.version_local,
            resolved_path = excluded.resolved_path,
            subfolder = excluded.subfolder,
            flatten = excluded.flatten,
            protected_files = excluded.protected_files,
            remote_status = excluded.remote_status,
            is_special = excluded.is_special,
            is_watching = excluded.is_watching,
            is_licensed = excluded.is_licensed,
            is_explicit = excluded.is_explicit,
            is_dependency = excluded.is_dependency
        """,
        (
            target.target_key,
            int(target.resource_id),
            int(target.parent_id) if target.parent_id is not None else 0,
            target.parent_target_key,
            int(target.root_resource_id),
            target.target_kind,
            persisted_category_id,
            persisted_title,
            remote_state.version_id if remote_state else None,
            version_local,
            persisted_resolved_path,
            persisted_subfolder,
            int(persisted_flatten),
            json.dumps(persisted_protected_files),
            remote_state.status if remote_state else None,
            flags["is_special"],
            flags["is_watching"],
            flags["is_licensed"],
            flags["is_explicit"],
            flags["is_dependency"],
        ),
    )


async def record_download_success(
    db_path: str,
    *,
    target: DesiredInstallTarget,
    action: PlannedAction,
    remote_state: RemoteResourceState,
) -> None:
    """Persist one target immediately after download so interrupted syncs keep progress."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await _upsert_download_row(
            conn,
            target=target,
            action=action,
            remote_state=remote_state,
            version_local=remote_state.version_id,
        )
        await conn.commit()


async def record_installed_state(
    db_path: str,
    *,
    desired_set: DesiredSet,
    remote_snapshot: RemoteSnapshot,
    local_snapshot: LocalSnapshot,
    execution_plan: ExecutionPlan,
    execution_result: ExecutionResult,
) -> None:
    """End-of-run batch write for all outcomes: skips, blocks, and untracks."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        for target_key, action in execution_plan.actions.items():
            result_item = execution_result.items[target_key]
            existing = local_snapshot.install_targets.get(target_key)

            if action.action == "untrack":
                await conn.execute("DELETE FROM downloads WHERE target_key = ?", (target_key,))
                continue

            if result_item.outcome == "downloaded":
                continue

            desired_target = desired_set.install_targets.get(target_key)
            if desired_target is None:
                continue

            remote_state = remote_snapshot.resources.get(action.resource_id)
            existing_local_version = existing.version_local if existing else None
            version_local = existing_local_version

            if result_item.outcome == "skipped" and remote_state and remote_state.version_id is not None:
                version_local = existing_local_version if existing_local_version is not None else remote_state.version_id

            await _upsert_download_row(
                conn,
                target=desired_target,
                action=action,
                remote_state=remote_state,
                version_local=version_local,
            )

        await conn.commit()


def reset_download_dates(cursor) -> None:
    reset_all_versions(cursor)
    cursor.execute("DELETE FROM navmesh_files")
    cursor.execute("DELETE FROM navmesh_cache")
    try:
        meta.clear_pypi_cache()
    except Exception:
        pass


def reset_download_dates_for_resources(db_name: str, resource_ids: Iterable[str]) -> bool:
    """Force re-download of selected resources without touching anything else."""
    try:
        with get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            for resource_id in resource_ids:
                reset_versions_for_resource(cursor, resource_id)
            conn.commit()
        return True
    except Exception as exc:
        print(f"Error resetting download dates: {exc}")
        return False


async def reset_download_dates_async(db_path: str) -> None:
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await conn.execute("UPDATE downloads SET version_local = 0")
        await conn.execute("DELETE FROM navmesh_files")
        await conn.execute("DELETE FROM navmesh_cache")
        await conn.commit()
    try:
        meta.clear_pypi_cache()
    except Exception:
        pass


def list_resources(cursor) -> list[tuple[int, str]]:
    cursor.execute(
        """
        SELECT resource_id, title
        FROM downloads
        WHERE parent_target_key IS NULL
        ORDER BY resource_id
        """
    )
    return cursor.fetchall()


def list_dependencies(cursor) -> list[tuple[int, str]]:
    cursor.execute(
        """
        SELECT resource_id, title
        FROM downloads
        WHERE parent_target_key IS NOT NULL
        ORDER BY root_resource_id, target_key
        """
    )
    return cursor.fetchall()


async def fetch_root_version_local(db_path: str, resource_id: str) -> int | None:
    """Return the local version stamp for a resource, or None if not installed."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        async with conn.execute(
            """
            SELECT version_local
            FROM downloads
            WHERE resource_id = ? AND parent_target_key IS NULL
            LIMIT 1
            """,
            (int(resource_id),),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
