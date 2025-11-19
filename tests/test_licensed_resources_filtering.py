"""Tests for licensed resources filtering and insertion behavior (async)."""
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from redfetch.sync import _process_licensed_resources as _process_licensed_resources_async


@pytest.fixture
def mock_conn():
    return MagicMock()


def make_license(resource_id: int, parent_category_id: int, title: str = "Licensed Resource") -> dict:
    return {
        'active': True,
        'start_date': '2024-01-01',
        'end_date': '2025-01-01',
        'license_id': 12345,
        'resource': {
            'resource_id': resource_id,
            'title': title,
            'Category': {'parent_category_id': parent_category_id},
            'current_files': [{
                'id': 101,
                'filename': 'package.zip',
                'download_url': 'https://example.com/file.zip',
                'hash': 'd41d8cd98f00b204e9800998ecf8427e',
            }],
        },
    }


@pytest.mark.parametrize(
    "env,expected_calls,expected_len,should_contain",
    [
        ("TEST", 0, 0, False),
        ("EMU", 0, 0, False),
        ("LIVE", 1, 1, True),
    ],
)
@patch('redfetch.sync.store.insert_prepared_resource', new_callable=AsyncMock)
def test_licensed_plugins_only_live(mock_insert, mock_conn, env, expected_calls, expected_len, should_contain):
    # Category 11 = plugins; only allowed on LIVE env per filtering
    mock_settings = MagicMock()
    mock_settings.ENV = env
    with patch('redfetch.sync.config.settings', mock_settings), \
         patch('redfetch.sync.config.CATEGORY_MAP', {8: 'macros', 11: 'plugins', 25: 'lua'}):
        lic = make_license(9999, 11, 'Test Licensed Plugin')
        result = asyncio.run(_process_licensed_resources_async(mock_conn, [lic]))

        assert mock_insert.call_count == expected_calls
        assert len(result) == expected_len
        contains = (None, 9999) in result
        assert contains is should_contain

        if expected_calls == 1:
            # Ensure Resource dataclass is marked licensed
            resource_arg = mock_insert.call_args[0][1]
            assert getattr(resource_arg, 'is_licensed', False) is True


@pytest.mark.parametrize(
    "env,category_id,resource_id",
    [
        ("LIVE", 8, 9998), ("TEST", 8, 9998), ("EMU", 8, 9998),
        ("LIVE", 25, 9997), ("TEST", 25, 9997), ("EMU", 25, 9997),
    ],
)
@patch('redfetch.sync.store.insert_prepared_resource', new_callable=AsyncMock)
def test_cross_compatible_resources_all_envs(mock_insert, mock_conn, env, category_id, resource_id):
    # Categories 8 (macros) and 25 (lua) allowed across all envs
    mock_settings = MagicMock()
    mock_settings.ENV = env
    with patch('redfetch.sync.config.settings', mock_settings), \
         patch('redfetch.sync.config.CATEGORY_MAP', {8: 'macros', 11: 'plugins', 25: 'lua'}):
        lic = make_license(resource_id, category_id, 'Cross Compatible')
        result = asyncio.run(_process_licensed_resources_async(mock_conn, [lic]))

        assert mock_insert.call_count == 1
        assert len(result) == 1
        assert (None, resource_id) in result

        resource_arg = mock_insert.call_args[0][1]
        assert getattr(resource_arg, 'is_licensed', False) is True


