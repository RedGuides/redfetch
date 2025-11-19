"""End-to-end cancellation tests for download pipeline.

Covers early cancellation (before queue build) and mid-download cancellation.
"""
import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from redfetch import store, sync


class WorkerStub:
    def __init__(self):
        self.is_cancelled = False


@pytest.fixture
def temp_db():
    temp_dir = tempfile.mkdtemp()
    db_name = "LIVE_resources.db"
    db_path = os.path.join(temp_dir, '.cache', db_name)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    yield db_path, db_name, temp_dir
    try:
        if os.path.exists(db_path):
            os.remove(db_path)
        cache_dir = os.path.dirname(db_path)
        if os.path.exists(cache_dir):
            os.rmdir(cache_dir)
        os.rmdir(temp_dir)
    except Exception:
        pass


def _make_resource(resource_id: int, title: str, filename: str) -> dict:
    return {
        'resource_id': resource_id,
        'title': title,
        'Category': {'parent_category_id': 11},
        'current_files': [{
            'id': resource_id * 10,
            'filename': filename,
            'download_url': f'https://example.com/{filename}',
            'hash': 'abc123',
        }],
    }


def test_cancel_before_queue_build(temp_db):
    db_path, db_name, temp_dir = temp_db

    # Minimal settings mock
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    mock_settings.from_env.return_value = MagicMock(
        ENV='LIVE',
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
        DOWNLOAD_FOLDER=temp_dir,
    )

    # Two watched resources to ensure there would be work
    watched = [
        _make_resource(1001, 'Res A', 'a.zip'),
        _make_resource(1002, 'Res B', 'b.zip'),
    ]

    async def _fetch_watched(client):
        return watched

    async def _fetch_licenses(client):
        return []

    async def _fetch_manifest(client):
        return {'resources': {}}

    # Initialize DB at the exact path used by the async pipeline
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    store._ensure_metadata(cur)
    store._ensure_downloads_table(cur)
    conn.commit()
    conn.close()

    with patch('redfetch.config.settings', mock_settings), \
         patch('redfetch.config.CATEGORY_MAP', {11: 'plugins'}), \
         patch('redfetch.store.get_db_path', return_value=db_path), \
         patch('redfetch.api.fetch_watched_resources', side_effect=_fetch_watched), \
         patch('redfetch.api.fetch_licenses', side_effect=_fetch_licenses), \
         patch('redfetch.net.fetch_manifest_cached', side_effect=_fetch_manifest), \
         patch('redfetch.download.download_file_async') as mock_dl, \
         patch('redfetch.download.extract_and_discard_zip'), \
         patch('redfetch.sync._fetch_from_api_async', side_effect=asyncio.CancelledError):

        # When cancellation happens before queue build, run_sync should treat it as a user cancel
        ok = asyncio.run(sync.run_sync(db_path, headers={}, resource_ids=None))
        assert ok is False
        # Ensure no downloads were attempted
        assert mock_dl.call_count == 0


def test_cancel_during_download_stream(temp_db):
    db_path, db_name, temp_dir = temp_db

    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    mock_settings.from_env.return_value = MagicMock(
        ENV='LIVE',
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
        DOWNLOAD_FOLDER=temp_dir,
    )

    # Single watched resource ensures one download starts, then we cancel
    watched = [_make_resource(2001, 'Res C', 'c.zip')]

    async def _fetch_watched(client):
        return watched

    async def _fetch_licenses(client):
        return []

    async def _fetch_manifest(client):
        return {'resources': {}}

    # Initialize DB at the exact path used by the async pipeline
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    store._ensure_metadata(cur)
    store._ensure_downloads_table(cur)
    conn.commit()
    conn.close()

    async def _inner():
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def _mock_download_file_async(client, url, path, md5=None):
            # Signal that the download started
            started.set()
            # Wait until test cancels
            await proceed.wait()
            # Emulate cooperative cancellation check raising
            raise asyncio.CancelledError()

        with patch('redfetch.config.settings', mock_settings), \
             patch('redfetch.config.CATEGORY_MAP', {11: 'plugins'}), \
             patch('redfetch.store.get_db_path', return_value=db_path), \
             patch('redfetch.api.fetch_watched_resources', side_effect=_fetch_watched), \
             patch('redfetch.api.fetch_licenses', side_effect=_fetch_licenses), \
             patch('redfetch.net.fetch_manifest_cached', side_effect=_fetch_manifest), \
             patch('redfetch.download.download_file_async', side_effect=_mock_download_file_async) as mock_dl, \
             patch('redfetch.download.extract_and_discard_zip'):

            task = asyncio.create_task(sync.run_sync(db_path, headers={}, resource_ids=None))
            # Wait until the download actually starts
            await started.wait()
            # Trigger cancellation and let the stub proceed to raise
            proceed.set()
            ok = await task

            return ok, mock_dl.call_count

    ok, calls = asyncio.run(_inner())
    assert ok is False
    assert calls == 1


