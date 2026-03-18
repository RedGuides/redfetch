"""Planner-level tests for sync queue eligibility."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from redfetch import store, sync


def make_resource_payload(
    resource_id: int,
    *,
    version_id: int = 101,
    file_id: int = 10001,
    category_id: int = 8,
    title: str | None = None,
) -> dict:
    return {
        "resource_id": resource_id,
        "title": title or f"Resource {resource_id}",
        "Category": {"parent_category_id": category_id},
        "current_files": [
            {
                "id": file_id,
                "filename": f"{resource_id}.zip",
                "download_url": f"https://example.com/{resource_id}.zip",
                "hash": "d41d8cd98f00b204e9800998ecf8427e",
            }
        ],
        "current_version": {"version_id": version_id},
    }


def make_fetched_data(*, special_status: dict, special_payloads: list[dict]) -> dict:
    return {
        "watched_resources": [],
        "licensed_resources": [],
        "special_resource_status": special_status,
        "special_resources_data": special_payloads,
    }


@pytest.fixture
def db_path(tmp_path) -> str:
    path = tmp_path / "queue_rules.db"
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    store._ensure_metadata(cursor)
    store._ensure_downloads_table(cursor)
    conn.commit()
    conn.close()
    return str(path)


def seed_download_row(
    db_path: str,
    *,
    resource_id: int,
    parent_id: int = 0,
    version_remote: int = 50,
    version_local: int = 0,
    is_special: int = 0,
    is_watching: int = 0,
    is_licensed: int = 0,
    category_id: int = 8,
    title: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO downloads (
                resource_id, parent_id, category_id, title,
                version_remote, version_local, filename, url, hash,
                is_special, is_watching, is_licensed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id,
                parent_id,
                category_id,
                title or f"Seeded {resource_id}",
                version_remote,
                version_local,
                f"{resource_id}.zip",
                f"https://stale.example/{resource_id}.zip",
                "d41d8cd98f00b204e9800998ecf8427e",
                is_special,
                is_watching,
                is_licensed,
            ),
        )
        conn.commit()


def run_sync_and_capture_queue(
    db_path: str,
    *,
    fetched_data: dict,
    resource_ids: list[str] | None = None,
) -> tuple[bool, list[tuple[str, str | None]], int]:
    queued: list[tuple[str, str | None]] = []

    async def fake_download_and_update(_db_path, _headers, to_download, _on_event):
        queued.extend((task.resource_id, task.parent_resource_id) for task in to_download)
        return [(task.resource_id, "downloaded") for task in to_download]

    with patch(
        "redfetch.sync._fetch_from_api_async",
        new=AsyncMock(return_value=fetched_data),
    ), patch(
        "redfetch.sync._download_and_update",
        new=AsyncMock(side_effect=fake_download_and_update),
    ) as download_mock:
        result = asyncio.run(sync.sync(db_path, headers={}, resource_ids=resource_ids))

    return result, queued, download_mock.await_count


def test_queue_allows_downloadable_special_resource(db_path):
    special_status = {
        "151": {"is_special": True, "is_dependency": False, "parent_ids": set()},
    }
    fetched_data = make_fetched_data(
        special_status=special_status,
        special_payloads=[
            make_resource_payload(
                151,
                version_id=2001,
                file_id=92001,
                title="Downloadable Special",
            )
        ],
    )

    result, queued, await_count = run_sync_and_capture_queue(db_path, fetched_data=fetched_data)

    assert result is True
    assert queued == [("151", None)]
    assert await_count == 1


def test_queue_blocks_special_resource_when_user_lacks_access(db_path):
    special_status = {
        "151": {"is_special": True, "is_dependency": False, "parent_ids": set()},
    }
    fetched_data = make_fetched_data(
        special_status=special_status,
        special_payloads=[],
    )

    result, queued, await_count = run_sync_and_capture_queue(db_path, fetched_data=fetched_data)

    assert result is True
    assert queued == []
    assert await_count == 0


def test_sync_aborts_before_queue_when_metadata_fetch_fails(db_path):
    seed_download_row(
        db_path,
        resource_id=151,
        version_remote=99,
        version_local=0,
        is_special=1,
    )

    with patch(
        "redfetch.sync._fetch_from_api_async",
        new=AsyncMock(side_effect=RuntimeError("metadata exploded")),
    ), patch(
        "redfetch.sync._download_and_update",
        new=AsyncMock(),
    ) as download_mock:
        with pytest.raises(RuntimeError, match="metadata exploded"):
            asyncio.run(sync.sync(db_path, headers={}, resource_ids=None))

    assert download_mock.await_count == 0


@pytest.mark.xfail(
    reason="Current sync logic can re-queue a stale special row even when no fresh downloadable metadata exists."
)
def test_queue_blocks_stale_special_row_without_fresh_metadata(db_path):
    seed_download_row(
        db_path,
        resource_id=151,
        version_remote=77,
        version_local=0,
        is_special=1,
    )

    special_status = {
        "151": {"is_special": True, "is_dependency": False, "parent_ids": set()},
    }
    fetched_data = make_fetched_data(
        special_status=special_status,
        special_payloads=[],
    )

    result, queued, await_count = run_sync_and_capture_queue(db_path, fetched_data=fetched_data)

    assert result is True
    assert queued == []
    assert await_count == 0


@pytest.mark.xfail(
    reason="Current sync logic can queue a dependency when only a stale parent root row remains."
)
def test_queue_blocks_dependency_when_parent_is_blocked(db_path):
    seed_download_row(
        db_path,
        resource_id=151,
        version_remote=50,
        version_local=50,
        is_special=1,
    )

    special_status = {
        "151": {"is_special": True, "is_dependency": False, "parent_ids": set()},
        "1865": {"is_special": False, "is_dependency": True, "parent_ids": {"151"}},
    }
    fetched_data = make_fetched_data(
        special_status=special_status,
        special_payloads=[
            make_resource_payload(
                1865,
                version_id=1234,
                file_id=81234,
                title="Dependency Only",
            )
        ],
    )

    result, queued, await_count = run_sync_and_capture_queue(db_path, fetched_data=fetched_data)

    assert result is True
    assert queued == []
    assert await_count == 0
