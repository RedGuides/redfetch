from __future__ import annotations

# standard
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
import json
import os
import sqlite3

# third-party
import aiosqlite
from pydantic import TypeAdapter, ValidationError

# local
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


SCHEMA_VERSION = 3


def db_name(env: str) -> str:
    """Per-env resource DB filename — the one definition of the naming scheme."""
    return f"{env}_resources.db"


def get_db_path(db_name: str) -> str:
    return os.path.join(config.cache_dir(), db_name)


def _apply_connection_pragmas(conn) -> None:
    # Set SQLite to WAL mode, 5s busy timeout, and NORMAL sync. WAL persists for all connections.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass


@contextmanager
def get_db_connection(db_name: str) -> Iterator[sqlite3.Connection]:
    """Yield an autocommit connection, closed on exit."""
    db_path = get_db_path(db_name)
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False, isolation_level=None)
    _apply_connection_pragmas(conn)
    try:
        yield conn
    finally:
        conn.close()


def initialize_db(db_name: str):
    with get_db_connection(db_name) as conn:
        initialize_schema(conn.cursor())


def _ensure_metadata(cursor) -> None:
    try:
        cursor.execute("SELECT schema_version FROM metadata WHERE id = 1")
        row = cursor.fetchone()
        version = int(row[0]) if row and row[0] is not None else 0
        if version >= SCHEMA_VERSION:
            return
        if version == 2:
            _migrate_v2_add_server_slug(cursor)
            return
    except Exception:
        pass
    _reset_sync_schema(cursor)


_V2_COLUMNS = (
    "target_key, resource_id, parent_id, parent_target_key, root_resource_id, "
    "target_kind, category_id, title, version_remote, version_local, "
    "resolved_path, subfolder, flatten, protected_files, remote_status, "
    "is_special, is_watching, is_licensed, is_explicit, is_dependency"
)


def _migrate_v2_add_server_slug(cursor) -> None:
    """v2 -> v3: re-key rows by (target_key, server_slug), keeping install state.

    Migrated rows get slug '' — the first server write under a slug promotes
    its row (see _upsert_download_row), so no attribution pass is needed.
    """
    cursor.execute("ALTER TABLE downloads RENAME TO downloads_v2")
    _ensure_downloads_table(cursor)
    cursor.execute(
        f"INSERT INTO downloads ({_V2_COLUMNS}) SELECT {_V2_COLUMNS} FROM downloads_v2"
    )

    cursor.execute("DROP TABLE downloads_v2")
    cursor.execute("UPDATE metadata SET schema_version = ? WHERE id = 1", (SCHEMA_VERSION,))


def _ensure_downloads_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY,
            target_key TEXT NOT NULL,
            server_slug TEXT NOT NULL DEFAULT '',
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
            is_dependency INTEGER NOT NULL DEFAULT 0,
            UNIQUE(target_key, server_slug)
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
    # drop the legacy table
    cursor.execute("DROP TABLE IF EXISTS navmesh_cache")


def _reset_sync_schema(cursor) -> None:
    cursor.execute("DROP TABLE IF EXISTS downloads")
    cursor.execute("DROP TABLE IF EXISTS downloads_v2")
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


def reset_versions_for_resource(cursor, resource_id: str, server_slug: str = "") -> None:
    """Force a re-download of one resource's rows — the active server's, not every server's."""
    key = f"/{resource_id}/"
    cursor.execute(
        """
        UPDATE downloads
        SET version_local = 0
        WHERE (target_key = ? OR target_key LIKE ?)
          AND server_slug IN ('', ?)
        """,
        (
            key,
            f"{key}%",
            server_slug,
        ),
    )


_PROTECTED_FILES = TypeAdapter(list[str])


def _decode_protected_files(raw: str | None) -> list[str]:
    # corrupt or non-list-of-strings JSON degrades to [] (no protected files)
    try:
        return _PROTECTED_FILES.validate_json(raw or "[]")
    except ValidationError:
        return []


def _row_server_slug(root_resource_id: str, server_slug: str) -> str:
    """Only maps packs live under each server's eqpath, so only their rows key per server."""
    if str(root_resource_id) in config.MAPS_MAP.values():
        return server_slug
    return ""


def _row_to_local_state(row: sqlite3.Row) -> LocalInstallState:
    return LocalInstallState(
        target_key=str(row["target_key"]),
        server_slug=str(row["server_slug"]),
        resource_id=str(row["resource_id"]),
        parent_id=str(row["parent_id"]) if row["parent_id"] not in (None, 0) else None,
        parent_target_key=row["parent_target_key"],
        root_resource_id=str(row["root_resource_id"]),
        target_kind=str(row["target_kind"]),
        category_id=row["category_id"],
        title=row["title"],
        version_local=row["version_local"],
        version_remote=row["version_remote"],
        resolved_path=row["resolved_path"],
        subfolder=row["subfolder"],
        flatten=bool(row["flatten"]),
        protected_files=_decode_protected_files(row["protected_files"]),
        is_special=bool(row["is_special"]),
        is_watching=bool(row["is_watching"]),
        is_licensed=bool(row["is_licensed"]),
        is_explicit=bool(row["is_explicit"]),
        is_dependency=bool(row["is_dependency"]),
    )


async def load_local_snapshot(db_path: str, server_slug: str = "") -> LocalSnapshot:
    """Load all tracked install targets from the DB so the planner knows what's installed."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT
                target_key, server_slug, resource_id, parent_id, parent_target_key, root_resource_id,
                target_kind, category_id, title, version_remote, version_local,
                resolved_path, subfolder, flatten, protected_files,
                is_special, is_watching, is_licensed, is_explicit, is_dependency
            FROM downloads
            WHERE server_slug IN ('', ?)
            ORDER BY server_slug
            """,
            (server_slug,),
        ) as cursor:
            rows = await cursor.fetchall()

    return LocalSnapshot(
        install_targets={
            str(row["target_key"]): _row_to_local_state(row)
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


def _upsert(table: str, row: dict, key: tuple[str, ...]) -> tuple[str, tuple]:
    """One named dict drives the column list, placeholders, and update set."""
    sets = ", ".join(f"{c} = excluded.{c}" for c in row if c not in key)
    return (
        f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))}) "
        f"ON CONFLICT({', '.join(key)}) DO UPDATE SET {sets}",
        tuple(row.values()),
    )


async def _upsert_download_row(
    conn: aiosqlite.Connection,
    *,
    target: DesiredInstallTarget,
    action: PlannedAction | None,
    remote_state: RemoteResourceState | None,
    version_local: int | None,
    server_slug: str = "",
) -> None:
    """Save or overwrite a single download row with the best-known values from each source."""
    row_slug = _row_server_slug(target.root_resource_id, server_slug)
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
    row = {
        "target_key": target.target_key,
        "server_slug": row_slug,
        "resource_id": int(target.resource_id),
        "parent_id": int(target.parent_id) if target.parent_id is not None else 0,
        "parent_target_key": target.parent_target_key,
        "root_resource_id": int(target.root_resource_id),
        "target_kind": target.target_kind,
        "category_id": persisted_category_id,
        "title": persisted_title,
        "version_remote": remote_state.version_id if remote_state else None,
        "version_local": version_local,
        "resolved_path": persisted_resolved_path,
        "subfolder": persisted_subfolder,
        "flatten": int(persisted_flatten),
        "protected_files": json.dumps(persisted_protected_files),
        "remote_status": remote_state.status if remote_state else None,
        **flags,
    }
    await conn.execute(*_upsert("downloads", row, key=("target_key", "server_slug")))
    if row_slug:
        await conn.execute(
            "DELETE FROM downloads WHERE target_key = ? AND server_slug = ''",
            (target.target_key,),
        )


async def record_download_success(
    db_path: str,
    *,
    target: DesiredInstallTarget,
    action: PlannedAction,
    remote_state: RemoteResourceState,
    server_slug: str = "",
) -> None:
    """Persist one target immediately after download so interrupted syncs keep progress."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await _upsert_download_row(
            conn,
            target=target,
            action=action,
            remote_state=remote_state,
            version_local=remote_state.version_id,
            server_slug=server_slug,
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
    server_slug: str = "",
) -> None:
    """End-of-run batch write for all outcomes: skips, blocks, and untracks."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        for target_key, action in execution_plan.actions.items():
            result_item = execution_result.items[target_key]
            existing = local_snapshot.install_targets.get(target_key)

            if action.action == "untrack":
                # Scoped to the snapshot row's own slug: inactive servers' rows stay.
                await conn.execute(
                    "DELETE FROM downloads WHERE target_key = ? AND server_slug = ?",
                    (target_key, existing.server_slug if existing else ""),
                )
                continue

            if result_item.outcome == "downloaded":
                continue

            # Keep a failed download's row untouched
            if action.action == "download" and existing is not None:
                continue

            # A block must not restamp an existing row with this run's context
            if action.action == "block" and existing is not None:
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
                server_slug=server_slug,
            )

        await conn.commit()


def reset_download_dates(cursor) -> None:
    reset_all_versions(cursor)
    cursor.execute("DELETE FROM navmesh_files")
    with suppress(Exception):
        meta.clear_pypi_cache()  # cache invalidation is best-effort


def reset_download_dates_for_resources(db_name: str, resource_ids: Iterable[str], server_slug: str = "") -> bool:
    """Force re-download of selected resources without touching anything else."""
    try:
        with get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            for resource_id in resource_ids:
                reset_versions_for_resource(cursor, resource_id, server_slug)
            conn.commit()
        return True
    except Exception as exc:
        print(f"Error resetting download dates: {exc}")
        return False


def rekey_server_rows(db_name: str, old_slug: str, new_slug: str) -> None:
    """Follow a server rename so its per-server rows keep their install state."""
    if not old_slug or not new_slug:
        raise ValueError("server slug required")
    if not os.path.exists(get_db_path(db_name)):
        return
    with get_db_connection(db_name) as conn:
        # renames require an unused name
        conn.execute("DELETE FROM downloads WHERE server_slug = ?", (new_slug,))
        conn.execute(
            "UPDATE downloads SET server_slug = ? WHERE server_slug = ?",
            (new_slug, old_slug),
        )


def purge_server_rows(db_name: str, slug: str) -> None:
    """Delete a removed server's rows so they don't linger as unreachable orphans."""
    if not slug:
        raise ValueError("server slug required")
    if not os.path.exists(get_db_path(db_name)):
        return
    with get_db_connection(db_name) as conn:
        conn.execute("DELETE FROM downloads WHERE server_slug = ?", (slug,))


async def reset_download_dates_async(db_path: str) -> None:
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await conn.execute("UPDATE downloads SET version_local = 0")
        await conn.execute("DELETE FROM navmesh_files")
        await conn.commit()
    with suppress(Exception):
        meta.clear_pypi_cache()  # cache invalidation is best-effort


def list_resources(cursor) -> list[tuple[int, str]]:
    cursor.execute(
        """
        SELECT DISTINCT resource_id, title
        FROM downloads
        WHERE parent_target_key IS NULL
        ORDER BY resource_id
        """
    )
    return cursor.fetchall()


def list_dependencies(cursor) -> list[tuple[int, str]]:
    cursor.execute(
        """
        SELECT DISTINCT resource_id, title
        FROM downloads
        WHERE parent_target_key IS NOT NULL
        ORDER BY root_resource_id, target_key
        """
    )
    return cursor.fetchall()


async def fetch_root_version_local(db_path: str, resource_id: str, server_slug: str = "") -> int | None:
    """Return the local version stamp for a resource, or None if not installed."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        async with conn.execute(
            """
            SELECT version_local
            FROM downloads
            WHERE resource_id = ? AND parent_target_key IS NULL AND server_slug IN ('', ?)
            ORDER BY server_slug DESC
            LIMIT 1
            """,
            (int(resource_id), server_slug),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
