"""Minimal tests for licensed resources filtering by environment."""
import pytest
from unittest.mock import MagicMock, patch
from redfetch import main


@pytest.fixture
def mock_cursor():
    return MagicMock()


def make_resource(resource_id, parent_category_id, title):
    return {
        'active': True,
        'start_date': '2024-01-01',
        'end_date': '2025-01-01',
        'license_id': 12345,
        'resource': {
            'resource_id': resource_id,
            'title': title,
            'Category': {'parent_category_id': parent_category_id},
            'current_files': [{'version': '1.0'}],
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
@patch('redfetch.db.insert_prepared_resource')
def test_licensed_plugins_only_live(mock_insert, mock_cursor, env, expected_calls, expected_len, should_contain):
    mock_settings = MagicMock()
    mock_settings.ENV = env
    with patch('redfetch.main.config.settings', mock_settings):
        resource = make_resource(9999, 11, 'Test Licensed Plugin')
        result = main.process_licensed_resources(mock_cursor, [resource])

        assert mock_insert.call_count == expected_calls
        assert len(result) == expected_len
        if should_contain:
            assert (None, 9999) in result
        else:
            assert (None, 9999) not in result


@pytest.mark.parametrize(
    "env,category_id,resource_id",
    [
        ("LIVE", 8, 9998), ("TEST", 8, 9998), ("EMU", 8, 9998),
        ("LIVE", 25, 9997), ("TEST", 25, 9997), ("EMU", 25, 9997),
    ],
)
@patch('redfetch.db.insert_prepared_resource')
def test_cross_compatible_resources_all_envs(mock_insert, mock_cursor, env, category_id, resource_id):
    mock_settings = MagicMock()
    mock_settings.ENV = env
    with patch('redfetch.main.config.settings', mock_settings):
        resource = make_resource(resource_id, category_id, 'Cross Compatible')
        result = main.process_licensed_resources(mock_cursor, [resource])

        assert mock_insert.call_count == 1
        assert len(result) == 1
        assert (None, resource_id) in result

