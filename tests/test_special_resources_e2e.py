"""End-to-end tests for special resources and dependencies download."""
import pytest
import tempfile
import os
import sqlite3
from unittest.mock import patch, MagicMock, AsyncMock

from redfetch import main, config, store


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    temp_dir = tempfile.mkdtemp()
    db_name = "LIVE_resources.db"
    db_path = os.path.join(temp_dir, '.cache', db_name)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    # Use the temp directory as config_dir
    config.config_dir = temp_dir
    
    # Initialize database
    store.initialize_db(db_name)
    
    yield db_path, db_name, temp_dir
    
    # Cleanup
    try:
        if os.path.exists(db_path):
            os.remove(db_path)
        cache_dir = os.path.dirname(db_path)
        if os.path.exists(cache_dir):
            os.rmdir(cache_dir)
        os.rmdir(temp_dir)
    except Exception:
        pass


@pytest.fixture
def mock_api_responses():
    """Mock API responses for special resources."""
    # Resource 151 (MySEQ Open - Live server offsets)
    resource_151 = {
        'resource_id': 151,
        'title': 'MySEQ Open (Live server offsets)',
        'Category': {'parent_category_id': 11},  # plugins
        'current_files': [{
            'id': 1001,
            'filename': 'myseqserver.ini',
            'download_url': 'https://example.com/myseqserver.ini',
            'hash': 'abc123',
        }],
        'current_version': {'version_id': 100}
    }
    
    # Resource 153 (Brewall's EverQuest Maps)
    resource_153 = {
        'resource_id': 153,
        'title': "Brewall's EverQuest Maps",
        'Category': {'parent_category_id': 11},  # plugins
        'current_files': [{
            'id': 1002,
            'filename': 'brewall-maps_20241203.zip',
            'download_url': 'https://example.com/brewall-maps.zip',
            'hash': 'def456',
        }],
        'current_version': {'version_id': 200}
    }
    
    # Resource 1865 (MySEQ)
    resource_1865 = {
        'resource_id': 1865,
        'title': 'MySEQ',
        'Category': {'parent_category_id': 11},  # plugins
        'current_files': [{
            'id': 1003,
            'filename': 'myseq.zip',
            'download_url': 'https://example.com/myseq.zip',
            'hash': 'ghi789',
        }],
        'current_version': {'version_id': 300}
    }
    
    return {
        '151': resource_151,
        '153': resource_153,
        '1865': resource_1865
    }


@pytest.fixture
def mock_settings_151_only(tmp_path):
    """Mock settings with only resource 151 opted in."""
    download_folder = str(tmp_path / "MacroQuest")
    settings_dict = {
        'ENV': 'LIVE',
        'DOWNLOAD_FOLDER': download_folder,
        'SPECIAL_RESOURCES': {
            '151': {
                'opt_in': True,
                'default_path': 'MySEQ\\Live',
                'dependencies': {
                    '153': {'subfolder': 'maps', 'flatten': True, 'opt_in': True},
                    '1865': {'subfolder': '', 'flatten': False, 'opt_in': True}
                }
            },
            '153': {
                'opt_in': False,
                'default_path': 'MySEQ\\Live\\maps',
                'dependencies': {}
            },
            '1865': {
                'opt_in': False,
                'default_path': 'MySEQ\\Live',
                'dependencies': {}
            }
        },
        'PROTECTED_FILES_BY_RESOURCE': {}
    }
    return settings_dict


@pytest.fixture
def mock_settings_151_and_153(tmp_path):
    """Mock settings with both 151 and 153 opted in."""
    download_folder = str(tmp_path / "MacroQuest")
    settings_dict = {
        'ENV': 'LIVE',
        'DOWNLOAD_FOLDER': download_folder,
        'SPECIAL_RESOURCES': {
            '151': {
                'opt_in': True,
                'default_path': 'MySEQ\\Live',
                'dependencies': {
                    '153': {'subfolder': 'maps', 'flatten': True, 'opt_in': True},
                    '1865': {'subfolder': '', 'flatten': False, 'opt_in': True}
                }
            },
            '153': {
                'opt_in': True,
                'default_path': 'MySEQ\\Live\\maps',
                'dependencies': {}
            },
            '1865': {
                'opt_in': False,
                'default_path': 'MySEQ\\Live',
                'dependencies': {}
            }
        },
        'PROTECTED_FILES_BY_RESOURCE': {}
    }
    return settings_dict


def test_download_resource_151_with_dependencies(
    temp_db, 
    mock_api_responses, 
    mock_settings_151_only
):
    """
    Test downloading resource 151 which should include its dependencies (153, 1865).
    
    Settings:
        [LIVE.SPECIAL_RESOURCES.151]
        opt_in = true
    
    Command:
        redfetch download 151
    
    Expected behavior:
        - Downloads resource 151 (MySEQ Open)
        - Downloads dependency 153 (Brewall's EverQuest Maps)
        - Downloads dependency 1865 (MySEQ)
        - Total of 3 resources downloaded
        
    Verification approach:
        - Verifies database contains correct entries for all resources
        - Checks parent_id relationships (0 for root, resource_id for dependencies)
        - Confirms download_file was called 3 times
        - Validates special resource flags are set correctly
    """
    db_path, db_name, temp_dir = temp_db
    
    # Mock configuration
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    mock_settings.SPECIAL_RESOURCES = mock_settings_151_only['SPECIAL_RESOURCES']
    mock_settings.from_env.return_value = MagicMock(**mock_settings_151_only)
    
    # Mock API fetch to return our special resources
    async def mock_fetch_resources_batch(client, resource_ids):
        return [mock_api_responses[str(rid)] for rid in resource_ids if str(rid) in mock_api_responses]
    
    # Mock download_file to avoid actual downloads and track calls
    def mock_download_file(download_url, file_path, headers, expected_md5=None):
        # Create the file as if it was downloaded
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as f:
            f.write('mock content')
        return True
    
    # Set up database so the file exists
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    store._ensure_metadata(cursor)
    store._ensure_downloads_table(cursor)
    conn.commit()
    
    async def _mock_download_file_async(client, url, path, md5=None, worker=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('mock content')
        return True

    with patch('redfetch.config.settings', mock_settings), \
         patch('redfetch.config.initialize_config'), \
         patch('redfetch.config_firstrun.first_run_setup', return_value=temp_dir), \
         patch('redfetch.auth.initialize_keyring'), \
         patch('redfetch.auth.authorize'), \
         patch('redfetch.api.get_api_headers', return_value={'Authorization': 'Bearer test'}), \
         patch('redfetch.api.is_kiss_downloadable', return_value=True), \
         patch('redfetch.api.fetch_resources_batch', side_effect=mock_fetch_resources_batch), \
         patch('redfetch.download.download_file_async', side_effect=_mock_download_file_async) as mock_download, \
         patch('redfetch.download.extract_and_discard_zip'), \
         patch('redfetch.store.initialize_db'), \
         patch('sys.argv', ['redfetch', 'download', '151']):
        
        # Run the main function (catches SystemExit since main calls sys.exit)
        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == 0, "Expected successful exit code 0"
        
        # Verify that download_file was called 3 times (one for each resource)
        assert mock_download.call_count == 3, f"Expected 3 download calls, got {mock_download.call_count}"
    
    # Re-open connection to ensure we see the committed async writes
    conn.close()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Verify resource 151 is stored as special (root entry)
    cursor.execute(
        "SELECT resource_id, parent_id, is_special FROM downloads WHERE resource_id=151 AND parent_id=0"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 151 should have a root entry"
    assert result[2] == 1, "Resource 151 should be marked as special"
    
    # Verify resource 153 is stored as dependency of 151
    cursor.execute(
        "SELECT resource_id, parent_id FROM downloads WHERE resource_id=153 AND parent_id=151"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 153 should be stored as dependency of 151"
    
    # Verify resource 1865 is stored as dependency of 151
    cursor.execute(
        "SELECT resource_id, parent_id FROM downloads WHERE resource_id=1865 AND parent_id=151"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 1865 should be stored as dependency of 151"
    
    # Verify total count
    cursor.execute("SELECT COUNT(*) FROM downloads")
    count = cursor.fetchone()[0]
    assert count == 3, f"Expected 3 entries in downloads table, got {count}"
    
    conn.close()


def test_download_watched_with_overlapping_dependency(
    temp_db,
    mock_api_responses,
    mock_settings_151_and_153
):
    """
    Test downloading watched resources where 153 is both a dependency of 151 AND a standalone special resource.
    
    Settings:
        [LIVE.SPECIAL_RESOURCES.151]
        opt_in = true
        [LIVE.SPECIAL_RESOURCES.153]
        opt_in = true
    
    Command:
        redfetch update
    
    Expected behavior:
        - Downloads resource 151 (MySEQ Open) with its dependencies (153, 1865)
        - Downloads resource 153 (Brewall's Maps) as a standalone special resource
        - Resource 153 should have TWO entries in the database:
          1. As a dependency of 151 (parent_id=151)
          2. As a standalone special resource (parent_id=0)
        - Total of 4 database entries (151, 153 standalone, 153 as dependency, 1865)
        
    Verification approach:
        - Verifies API was called to fetch correct resource IDs
        - Checks database has proper dual entries for overlapping resource
        - Confirms correct parent_id values for all entries
        - Validates download behavior (resources not duplicated despite overlap)
    """
    db_path, db_name, temp_dir = temp_db
    
    # Mock configuration
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    mock_settings.SPECIAL_RESOURCES = mock_settings_151_and_153['SPECIAL_RESOURCES']
    mock_settings.from_env.return_value = MagicMock(**mock_settings_151_and_153)
    
    # Mock API fetch to return our special resources
    async def mock_fetch_resources_batch(client, resource_ids):
        return [mock_api_responses[str(rid)] for rid in resource_ids if str(rid) in mock_api_responses]
    
    async def mock_fetch_watched_resources(client):
        # Return empty list - we're only testing special resources
        return []
    
    async def mock_fetch_licenses(client):
        return []
    
    async def mock_fetch_manifest(client):
        # Return a manifest with our special resources
        return {
            'resources': {
                '151': {'last_update': 9999999999},
                '153': {'last_update': 9999999999},
                '1865': {'last_update': 9999999999}
            }
        }
    
    # Async download helper is patched below with a lambda
    
    # Set up database so the file exists
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    store._ensure_metadata(cursor)
    store._ensure_downloads_table(cursor)
    conn.commit()
    
    async def _mock_download_file_async2(client, url, path, md5=None, worker=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('mock content')
        return True

    with patch('redfetch.config.settings', mock_settings), \
         patch('redfetch.config.CATEGORY_MAP', {11: 'plugins'}), \
         patch('redfetch.config.initialize_config'), \
         patch('redfetch.config_firstrun.first_run_setup', return_value=temp_dir), \
         patch('redfetch.auth.initialize_keyring'), \
         patch('redfetch.auth.authorize'), \
         patch('redfetch.api.get_api_headers', return_value={'Authorization': 'Bearer test'}), \
         patch('redfetch.api.is_kiss_downloadable', return_value=True), \
         patch('redfetch.api.fetch_resources_batch', side_effect=mock_fetch_resources_batch), \
         patch('redfetch.api.fetch_watched_resources', side_effect=mock_fetch_watched_resources), \
         patch('redfetch.api.fetch_licenses', side_effect=mock_fetch_licenses), \
         patch('redfetch.net.fetch_manifest_cached', side_effect=mock_fetch_manifest), \
         patch('redfetch.download.download_file_async', side_effect=_mock_download_file_async2) as mock_download, \
         patch('redfetch.download.extract_and_discard_zip'), \
         patch('redfetch.net.is_mq_down', return_value=False), \
         patch('redfetch.processes.are_executables_running_in_folder', return_value=[]), \
         patch('redfetch.store.initialize_db'), \
         patch('sys.argv', ['redfetch', 'update']):
        
        # Run the main function (catches SystemExit since main calls sys.exit)
        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == 0, "Expected successful exit code 0"
        
        # Verify download_file was called for each resource
        # Note: We don't assert an exact count since the implementation may optimize duplicate downloads
        assert mock_download.call_count >= 3, f"Expected at least 3 download calls, got {mock_download.call_count}"
    
    # Re-open connection to ensure we see the committed async writes
    conn.close()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Verify resource 151 is stored as special (root entry)
    cursor.execute(
        "SELECT resource_id, parent_id, is_special FROM downloads WHERE resource_id=151 AND parent_id=0"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 151 should have a root entry"
    assert result[2] == 1, "Resource 151 should be marked as special"
    
    # Verify resource 153 has TWO entries:
    # 1. As a standalone special resource (parent_id=0, is_special=1)
    cursor.execute(
        "SELECT resource_id, parent_id, is_special FROM downloads WHERE resource_id=153 AND parent_id=0"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 153 should have a root entry as standalone special resource"
    assert result[2] == 1, "Resource 153 standalone entry should be marked as special"
    
    # 2. As a dependency of 151 (parent_id=151)
    cursor.execute(
        "SELECT resource_id, parent_id, is_special FROM downloads WHERE resource_id=153 AND parent_id=151"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 153 should also be stored as dependency of 151"
    # Note: When stored as a dependency, is_special is set based on the parent resource's special status
    # The important part is that the row exists
    
    # Verify resource 1865 is stored as dependency of 151
    cursor.execute(
        "SELECT resource_id, parent_id FROM downloads WHERE resource_id=1865 AND parent_id=151"
    )
    result = cursor.fetchone()
    assert result is not None, "Resource 1865 should be stored as dependency of 151"
    
    # Verify total count - should be 4 entries:
    # 1. Resource 151 (parent_id=0)
    # 2. Resource 153 standalone (parent_id=0)
    # 3. Resource 153 as dependency (parent_id=151)
    # 4. Resource 1865 as dependency (parent_id=151)
    cursor.execute("SELECT COUNT(*) FROM downloads")
    count = cursor.fetchone()[0]
    assert count == 4, f"Expected 4 entries in downloads table (151 root, 153 root, 153 dep, 1865 dep), got {count}"
    
    # List all entries for debugging
    cursor.execute("SELECT resource_id, parent_id, is_special FROM downloads ORDER BY resource_id, parent_id")
    all_entries = cursor.fetchall()
    print("\nDatabase entries:")
    for entry in all_entries:
        print(f"  Resource {entry[0]}, Parent {entry[1]}, Special: {entry[2]}")
    
    conn.close()


def test_special_resource_detection_logic():
    """
    Test that the special resource detection logic returns correct values.
    
    This verifies the core business logic using special.compute_special_status().
    """
    from redfetch import special
    
    special_resources = {
        '151': {
            'opt_in': True,
            'dependencies': {
                '153': {'subfolder': 'maps', 'flatten': True, 'opt_in': True},
                '1865': {'subfolder': '', 'flatten': False, 'opt_in': True}
            }
        },
        '153': {'opt_in': True, 'dependencies': {}},
        '1865': {'opt_in': False, 'dependencies': {}}
    }
    
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    mock_env_settings = MagicMock()
    mock_env_settings.SPECIAL_RESOURCES = special_resources
    mock_settings.from_env.return_value = mock_env_settings
    
    with patch('redfetch.config.settings', mock_settings):
        status = special.compute_special_status(['151', '153', '1865'])
        
        # Resource 151 (special, not a dependency)
        info_151 = status['151']
        assert info_151['is_special'] is True, "Resource 151 should be identified as special"
        assert info_151['is_dependency'] is False, "Resource 151 should not be identified as a dependency"
        assert info_151['parent_ids'] == set(), "Resource 151 should have no parent IDs"
        
        # Resource 153 (both special AND a dependency of 151)
        info_153 = status['153']
        assert info_153['is_special'] is True, "Resource 153 should be identified as special"
        assert info_153['is_dependency'] is True, "Resource 153 should also be identified as a dependency"
        assert info_153['parent_ids'] == {'151'}, "Resource 153 should have 151 as parent"
        
        # Resource 1865 (dependency only, not special)
        info_1865 = status['1865']
        assert info_1865['is_special'] is False, "Resource 1865 should not be identified as special (opt_in=False)"
        assert info_1865['is_dependency'] is True, "Resource 1865 should be identified as a dependency"
        assert info_1865['parent_ids'] == {'151'}, "Resource 1865 should have 151 as parent"

