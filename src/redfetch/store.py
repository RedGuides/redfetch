from collections.abc import Iterable
import os
import sqlite3
import aiosqlite

from redfetch import config
from redfetch.models import DownloadTask, Resource
from redfetch import meta

# Unified schema version marker
SCHEMA_VERSION = 1


def _get_cache_dir() -> str:
    """Get the .cache directory path, creating it if needed."""
    base = getattr(config, 'config_dir', None) or os.getenv('REDFETCH_CONFIG_DIR')
    if not base:
        # As a last resort, place cache in current working directory
        base = os.getcwd()
    cache_dir = os.path.join(base, '.cache')
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_db_connection(db_name: str):
    """Open SQLite DB in .cache directory."""
    db_path = get_db_path(db_name)
    # Allow using the connection across threads (we still write from main thread),
    # increase timeout to wait for locks, and enable autocommit mode to keep locks short
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False, isolation_level=None)
    try:
        # WAL allows readers and writers concurrently; busy_timeout reduces 'database is locked' errors
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        # Pragmas are best-effort; continue even if not supported
        pass
    return conn


def get_db_path(db_name: str) -> str:
    """Compute absolute path to the SQLite DB file in .cache directory."""
    return os.path.join(_get_cache_dir(), db_name)


def initialize_db(db_name: str) -> None:
    """Ensure unified schema; reset once to unified schema if version is outdated."""
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        _ensure_metadata(cursor)
        _ensure_downloads_table(cursor)
        _normalize_parent_ids(cursor)
        _ensure_indexes(cursor)
        _ensure_navmesh_tables(cursor)


def _ensure_downloads_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY,
            resource_id INTEGER NOT NULL,
            parent_id INTEGER NOT NULL DEFAULT 0,
            category_id INTEGER,
            title TEXT,
            version_remote INTEGER,
            version_local INTEGER DEFAULT 0,
            filename TEXT,
            url TEXT,
            hash TEXT,
            is_special BOOLEAN DEFAULT 0,
            is_watching BOOLEAN DEFAULT 0,
            is_licensed BOOLEAN DEFAULT 0,
            UNIQUE(resource_id, parent_id)
        )
        """
    )


def _ensure_metadata(cursor) -> None:
    """Ensure metadata table exists and contains schema_version; migrate safely from older layouts."""
    # Create base metadata table (older versions had only these two columns)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            id INTEGER PRIMARY KEY,
            last_fetch_time INTEGER
        )
        """
    )
    # Ensure default row exists (compatible with old schema)
    cursor.execute(
        "INSERT INTO metadata (id, last_fetch_time) SELECT 1, 0 WHERE NOT EXISTS (SELECT 1 FROM metadata WHERE id = 1)"
    )
    # Ensure schema_version column exists, then set default
    cursor.execute("PRAGMA table_info(metadata)")
    cols = [row[1] for row in cursor.fetchall()]
    if 'schema_version' not in cols:
        cursor.execute("ALTER TABLE metadata ADD COLUMN schema_version INTEGER")
        cursor.execute("UPDATE metadata SET schema_version=0 WHERE id=1")
    # Check current version and reset schema if outdated
    cursor.execute("SELECT schema_version FROM metadata WHERE id=1")
    row = cursor.fetchone()
    current = int(row[0]) if row and row[0] is not None else 0
    if current < SCHEMA_VERSION:
        _reset_to_unified_schema(cursor)


def _ensure_indexes(cursor) -> None:
    """Create indexes."""
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_roots ON downloads(parent_id, resource_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_flags ON downloads(parent_id, is_watching, is_special, is_licensed)"
        )
    except Exception:
        # Best-effort; continue even if index creation fails
        pass


def _ensure_navmesh_tables(cursor) -> None:
    """Create navmesh tracking tables if they don't exist."""
    # NavMesh file tracking (local file state cache)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS navmesh_files (
            filename TEXT PRIMARY KEY,
            md5_hash TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        )
    """)

    # NavMesh manifest cache metadata (per environment)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS navmesh_cache (
            env TEXT PRIMARY KEY,
            etag TEXT,
            last_modified TEXT,
            manifest_json TEXT
        )
    """)

def _reset_to_unified_schema(cursor) -> None:
    # Drop any existing tables (legacy or previous unified) and recreate unified
    cursor.execute("DROP TABLE IF EXISTS downloads")
    cursor.execute("DROP TABLE IF EXISTS resources")
    cursor.execute("DROP TABLE IF EXISTS dependencies")
    cursor.execute(
        "UPDATE metadata SET last_fetch_time=0, schema_version=? WHERE id=1",
        (SCHEMA_VERSION,),
    )


def map_rows_to_download_tasks(rows: Iterable[tuple]) -> list[DownloadTask]:
    """Map DB rows to DownloadTask objects."""
    tasks = []
    for row in rows:
        resource_id, category_id, title, version_remote, version_local, parent_resource_id, url, filename, file_hash = row
        tasks.append(DownloadTask(
            resource_id=str(resource_id),
            title=title,
            category_id=category_id,
            remote_version=version_remote,
            local_version=version_local,
            parent_resource_id=str(parent_resource_id) if parent_resource_id is not None else None,
            download_url=url,
            filename=filename,
            file_hash=file_hash,
        ))
    return tasks


def reset_download_dates(cursor) -> None:
    """Reset all download dates to force re-download and re-fetch from API."""
    cursor.execute("UPDATE downloads SET version_local=0")
    cursor.execute("UPDATE metadata SET last_fetch_time=0 WHERE id=1")
    # Clear navmesh cache tables
    cursor.execute("DELETE FROM navmesh_files")
    cursor.execute("DELETE FROM navmesh_cache")
    try:
        meta.clear_pypi_cache()
    except Exception:
        pass


def reset_download_date_for_resource(cursor, resource_id: str) -> None:
    rid_int = int(resource_id)
    cursor.execute("UPDATE downloads SET version_local=0 WHERE resource_id=? OR parent_id=?", (rid_int, rid_int))


def reset_download_dates_for_resources(db_name: str, resource_ids: Iterable[str]) -> bool:
    """Reset download dates for the provided resource IDs."""
    try:
        with get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            for resource_id in resource_ids:
                reset_download_date_for_resource(cursor, resource_id)
            conn.commit()
        return True
    except Exception as exc:
        print(f"Error resetting download dates: {exc}")
        return False


async def reset_download_dates_async(db_path: str) -> None:
    """Async helper to reset all download dates and last_fetch_time for a DB path."""
    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            # Pragmas are best-effort
            pass
        await conn.execute("UPDATE downloads SET version_local=0")
        await conn.execute("UPDATE metadata SET last_fetch_time=0 WHERE id=1")
        # Clear navmesh cache tables
        await conn.execute("DELETE FROM navmesh_files")
        await conn.execute("DELETE FROM navmesh_cache")
        await conn.commit()
    # Clear PyPI cache outside the DB transaction
    try:
        meta.clear_pypi_cache()
    except Exception:
        pass


def list_resources(cursor) -> list[tuple[int, str]]:
    """Return (resource_id, title) for root resources."""
    cursor.execute("SELECT resource_id, title FROM downloads WHERE parent_id = 0")
    return cursor.fetchall()


def list_dependencies(cursor) -> list[tuple[int, str]]:
    """Return (resource_id, title) for dependency rows."""
    cursor.execute("SELECT resource_id, title FROM downloads WHERE parent_id != 0")
    return cursor.fetchall()


def _normalize_parent_ids(cursor) -> None:
    """Coalesce NULL parent_id to 0 and deduplicate rows before normalization."""
    # If table doesn't exist yet, nothing to do
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'")
    if not cursor.fetchone():
        return
    # Remove potential duplicate roots caused by NULL uniqueness behavior
    cursor.execute(
        """
        DELETE FROM downloads
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM downloads
            GROUP BY resource_id, COALESCE(parent_id, 0)
        )
        """
    )
    # Normalize NULL to 0 for roots
    cursor.execute("UPDATE downloads SET parent_id=0 WHERE parent_id IS NULL")


def get_root_version_local(cursor, resource_id: str) -> int | None:
    """Return version_local for a root (parent_id=0) resource, or None if absent."""
    try:
        cursor.execute(
            "SELECT version_local FROM downloads WHERE parent_id = 0 AND resource_id = ?",
            (int(resource_id),),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception:
        return None


# ===== Async Database Operations =====

async def apply_updates(db_path: str, updates: list[tuple[str, int, bool, str | None]]) -> None:
    """Apply version updates in one transaction using aiosqlite."""
    if not updates:
        return

    async with aiosqlite.connect(db_path, timeout=30.0) as conn:
        await conn.execute("BEGIN")
        try:
            for resource_id, remote_version, is_dependency, parent_resource_id in updates:
                if is_dependency and parent_resource_id:
                    await conn.execute(
                        "UPDATE downloads SET version_local=? WHERE resource_id=? AND parent_id=?",
                        (int(remote_version), int(resource_id), int(parent_resource_id)),
                    )
                else:
                    await conn.execute(
                        "UPDATE downloads SET version_local=? WHERE resource_id=? AND parent_id = 0",
                        (int(remote_version), int(resource_id)),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def get_last_fetch_time(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT last_fetch_time FROM metadata WHERE id = 1") as cur:
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


async def has_root_download_row_for_resource(conn: aiosqlite.Connection, resource_id: int) -> bool:
    try:
        async with conn.execute(
            "SELECT 1 FROM downloads WHERE resource_id = ? AND parent_id = 0 LIMIT 1",
            (int(resource_id),),
        ) as cur:
            return (await cur.fetchone()) is not None
    except Exception:
        return False


async def insert_prepared_resource(
    conn: aiosqlite.Connection,
    resource: Resource,
    is_special: bool,
    is_dependency: bool,
    parent_id: str | None,
    license_details: dict | None,
) -> tuple[int | None, int]:
    resource_id = resource.resource_id
    category_id = resource.category_id
    title = resource.title
    version_remote = resource.version
    filename = resource.file.filename
    url = resource.file.url
    file_hash = resource.file.hash
    is_watching = bool(getattr(resource, 'is_watching', False))
    is_licensed = bool(getattr(resource, 'is_licensed', False) or (license_details and license_details.get('active', False)))
    parent = int(parent_id) if (is_dependency and parent_id is not None) else 0

    await conn.execute(
        """
        INSERT INTO downloads (
            resource_id, parent_id, category_id, title,
            version_remote, filename, url, hash,
            is_special, is_watching, is_licensed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(resource_id, parent_id) DO UPDATE SET
            category_id=excluded.category_id,
            title=excluded.title,
            version_remote=excluded.version_remote,
            filename=excluded.filename,
            url=excluded.url,
            hash=excluded.hash,
            is_special=excluded.is_special,
            is_watching=excluded.is_watching,
            is_licensed=excluded.is_licensed
        """,
        (
            resource_id, parent, category_id, title,
            version_remote, filename, url, file_hash,
            int(bool(is_special) and not is_dependency), int(bool(is_watching)), int(bool(is_licensed))
        ),
    )

    if is_dependency:
        return (parent, int(resource_id))
    return (None, int(resource_id))


async def clean_up_unnecessary_resources(
    conn: aiosqlite.Connection,
    current_ids: set[tuple[int | None, int]],
):
    resource_ids = {int(rid) for pid, rid in current_ids if pid is None}
    parent_ids = {int(pid) for pid, rid in current_ids if pid is not None}
    all_resource_ids = resource_ids.union(parent_ids)
    dependency_pairs = {(int(pid), int(rid)) for pid, rid in current_ids if pid is not None}

    if all_resource_ids:
        placeholders = ','.join(['?'] * len(all_resource_ids))
        await conn.execute(
            f"UPDATE downloads SET is_watching=0 WHERE parent_id = 0 AND resource_id NOT IN ({placeholders})",
            tuple(all_resource_ids),
        )
        await conn.execute(
            f"DELETE FROM downloads WHERE parent_id = 0 AND is_licensed=1 AND resource_id NOT IN ({placeholders})",
            tuple(all_resource_ids),
        )
        await conn.execute(
            f"DELETE FROM downloads WHERE parent_id = 0 AND is_special=1 AND resource_id NOT IN ({placeholders})",
            tuple(all_resource_ids),
        )

    if dependency_pairs:
        placeholders = ', '.join(['(?, ?)'] * len(dependency_pairs))
        flat = [item for pair in dependency_pairs for item in pair]
        await conn.execute(
            f"DELETE FROM downloads WHERE parent_id != 0 AND (parent_id, resource_id) NOT IN ({placeholders})",
            flat,
        )

    await conn.execute("UPDATE metadata SET last_fetch_time = strftime('%s','now') WHERE id = 1")


async def fetch_watched_db_resources(conn: aiosqlite.Connection) -> list[DownloadTask]:
    async with conn.execute(
        """
        SELECT resource_id, category_id, title, version_remote, version_local,
               NULL as parent_resource_id, url, filename, hash
        FROM downloads
        WHERE parent_id = 0 AND (is_watching=1 OR is_special=1 OR is_licensed=1)
        """
    ) as cur:
        roots = await cur.fetchall()

    async with conn.execute(
        """
        SELECT d.resource_id, p.category_id as parent_category_id, d.title, d.version_remote, d.version_local,
               d.parent_id as parent_resource_id, d.url, d.filename, d.hash
        FROM downloads d
        JOIN downloads p ON p.resource_id=d.parent_id AND p.parent_id = 0
        WHERE d.parent_id != 0
        """
    ) as cur:
        deps = await cur.fetchall()

    if roots or deps:
        return map_rows_to_download_tasks(roots + deps)

    async with conn.execute(
        """
        SELECT resource_id, category_id, title, version_remote, version_local,
               NULL as parent_resource_id, url, filename, hash
        FROM downloads
        WHERE parent_id = 0
        """
    ) as cur:
        roots_all = await cur.fetchall()

    async with conn.execute(
        """
        SELECT d.resource_id, p.category_id as parent_category_id, d.title, d.version_remote, d.version_local,
               d.parent_id as parent_resource_id, d.url, d.filename, d.hash
        FROM downloads d
        JOIN downloads p ON p.resource_id=d.parent_id AND p.parent_id = 0
        WHERE d.parent_id != 0
        """
    ) as cur:
        deps_all = await cur.fetchall()
    return map_rows_to_download_tasks(roots_all + deps_all)


async def fetch_single_db_resource(conn: aiosqlite.Connection, resource_id: str) -> list[DownloadTask]:
    rid_int = int(resource_id)

    async with conn.execute(
        """
        SELECT resource_id, category_id, title, version_remote, version_local,
               NULL as parent_resource_id, url, filename, hash
        FROM downloads
        WHERE parent_id = 0 AND resource_id = ?
        """,
        (rid_int,),
    ) as cur:
        root = await cur.fetchone()

    async with conn.execute(
        """
        SELECT d.resource_id, p.category_id as parent_category_id, d.title, d.version_remote, d.version_local,
               d.parent_id as parent_resource_id, d.url, d.filename, d.hash
        FROM downloads d
        JOIN downloads p ON p.resource_id=d.parent_id AND p.parent_id = 0
        WHERE d.parent_id = ?
        """,
        (rid_int,),
    ) as cur:
        deps = await cur.fetchall()

    items = []
    if root:
        items.append(root)
    items.extend(deps)
    return map_rows_to_download_tasks(items)


async def has_dependency_rows(conn: aiosqlite.Connection, resource_id: int, parent_ids: set[int]) -> bool:
    """Check if all expected dependency rows exist for a resource."""
    if not parent_ids:
        return False
    placeholders = ",".join(["?"] * len(parent_ids))
    query = (
        f"SELECT COUNT(1) FROM downloads "
        f"WHERE resource_id=? AND parent_id IN ({placeholders})"
    )
    params = (int(resource_id), *[int(pid) for pid in parent_ids])
    async with conn.execute(query, params) as cur:
        row = await cur.fetchone()
        count = int(row[0]) if row and row[0] is not None else 0
    return count >= len(parent_ids)
