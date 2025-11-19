"""End-to-end tests for the uninstall command."""
import pytest
import tempfile
import os
from unittest.mock import patch, MagicMock
from io import StringIO

from redfetch import config, meta


@pytest.fixture
def temp_config_dirs():
    """Create temporary directories for testing config paths."""
    # Create temp directories for various paths
    temp_config = tempfile.mkdtemp()
    temp_download_live = tempfile.mkdtemp()
    temp_download_test = tempfile.mkdtemp()
    temp_eqpath = tempfile.mkdtemp()
    temp_special_resource = tempfile.mkdtemp()
    
    # Create a maps subdirectory in the EQPATH
    os.makedirs(os.path.join(temp_eqpath, "maps"), exist_ok=True)
    
    yield {
        'config': temp_config,
        'download_live': temp_download_live,
        'download_test': temp_download_test,
        'eqpath': temp_eqpath,
        'special_resource': temp_special_resource
    }
    
    # Cleanup
    for dir_path in [temp_config, temp_download_live, temp_download_test, temp_eqpath, temp_special_resource]:
        try:
            # Remove maps subdirectory if it exists
            maps_path = os.path.join(dir_path, "maps")
            if os.path.exists(maps_path):
                os.rmdir(maps_path)
            if os.path.exists(dir_path):
                os.rmdir(dir_path)
        except Exception:
            pass


def test_uninstall_basic(temp_config_dirs):
    """Test that uninstall command runs without errors."""
    # Mock config settings with multiple environments
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    
    # Create mock environment settings
    def mock_from_env(env):
        env_mock = MagicMock()
        if env == 'LIVE':
            env_mock.get.side_effect = lambda key, default=None: {
                'DOWNLOAD_FOLDER': temp_config_dirs['download_live'],
                'EQPATH': temp_config_dirs['eqpath'],
                'SPECIAL_RESOURCES': {}
            }.get(key, default)
        elif env == 'TEST':
            env_mock.get.side_effect = lambda key, default=None: {
                'DOWNLOAD_FOLDER': temp_config_dirs['download_test'],
                'EQPATH': None,
                'SPECIAL_RESOURCES': {}
            }.get(key, default)
        else:
            env_mock.get.side_effect = lambda key, default=None: {
                'DOWNLOAD_FOLDER': None,
                'EQPATH': None,
                'SPECIAL_RESOURCES': {}
            }.get(key, default)
        return env_mock
    
    mock_settings.from_env = mock_from_env
    
    with patch('redfetch.config.initialize_config') as mock_init_config, \
         patch('redfetch.config.settings', mock_settings), \
         patch('redfetch.meta.config.settings', mock_settings), \
         patch('redfetch.auth.logout') as mock_logout, \
         patch('redfetch.meta.get_executable_path', return_value=None), \
         patch('redfetch.meta.detect_installation_method', return_value='pip'), \
         patch('redfetch.meta.Confirm.ask', return_value=False), \
         patch('redfetch.meta.write_commands_to_file') as mock_write_commands, \
         patch.dict(os.environ, {'REDFETCH_CONFIG_DIR': temp_config_dirs['config']}), \
         patch('sys.stdout', new_callable=StringIO) as mock_stdout:
        
        # Call uninstall - expect SystemExit(0) at the end
        with pytest.raises(SystemExit) as exc_info:
            meta.uninstall()
        
        # Verify it's a clean exit
        assert exc_info.value.code == 0
        
        # Verify that initialize_config was called
        mock_init_config.assert_called_once()
        
        # Verify that logout was called
        mock_logout.assert_called_once()
        
        # Verify that write_commands_to_file was called (no popup in tests)
        assert mock_write_commands.called
        # Get the arguments passed to write_commands_to_file
        commands, paths = mock_write_commands.call_args[0]
        # Verify that paths include our test directories
        assert any(temp_config_dirs['download_live'] in str(p) for p in paths)
        
        # Check that some output was produced (manual cleanup instructions)
        output = mock_stdout.getvalue()
        assert len(output) > 0


def test_uninstall_with_special_resources(temp_config_dirs):
    """Test that uninstall handles special resources correctly."""
    # Mock config settings with special resources
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    
    # Create mock environment settings with special resources
    def mock_from_env(env):
        env_mock = MagicMock()
        if env == 'LIVE':
            env_mock.get.side_effect = lambda key, default=None: {
                'DOWNLOAD_FOLDER': temp_config_dirs['download_live'],
                'EQPATH': temp_config_dirs['eqpath'],
                'SPECIAL_RESOURCES': {
                    '151': {
                        'opt_in': True,
                        'custom_path': temp_config_dirs['special_resource'],
                        'default_path': 'MySEQ\\Live'
                    }
                }
            }.get(key, default)
        else:
            env_mock.get.side_effect = lambda key, default=None: {
                'DOWNLOAD_FOLDER': None,
                'EQPATH': None,
                'SPECIAL_RESOURCES': {}
            }.get(key, default)
        return env_mock
    
    mock_settings.from_env = mock_from_env
    
    with patch('redfetch.config.initialize_config') as mock_init_config, \
         patch('redfetch.config.settings', mock_settings), \
         patch('redfetch.meta.config.settings', mock_settings), \
         patch('redfetch.auth.logout') as mock_logout, \
         patch('redfetch.meta.get_executable_path', return_value=None), \
         patch('redfetch.meta.detect_installation_method', return_value='pip'), \
         patch('redfetch.meta.Confirm.ask', return_value=False), \
         patch('redfetch.meta.write_commands_to_file') as mock_write_commands, \
         patch.dict(os.environ, {'REDFETCH_CONFIG_DIR': temp_config_dirs['config']}), \
         patch('sys.stdout', new_callable=StringIO) as mock_stdout:
        
        # Call uninstall - expect SystemExit(0) at the end
        with pytest.raises(SystemExit) as exc_info:
            meta.uninstall()
        
        # Verify it's a clean exit
        assert exc_info.value.code == 0
        
        # Verify that initialize_config was called
        mock_init_config.assert_called_once()
        
        # Verify that logout was called
        mock_logout.assert_called_once()
        
        # Verify that write_commands_to_file was called (no popup in tests)
        assert mock_write_commands.called
        # Get the arguments passed to write_commands_to_file
        commands, paths = mock_write_commands.call_args[0]
        # Verify that paths include special resource path
        assert any(temp_config_dirs['special_resource'] in str(p) for p in paths)
        
        # Check that special resource path appears in output
        output = mock_stdout.getvalue()
        assert len(output) > 0


def test_uninstall_checks_all_environments(temp_config_dirs):
    """Test that uninstall checks all environments (DEFAULT, LIVE, TEST, EMU)."""
    # Mock config settings
    mock_settings = MagicMock()
    mock_settings.ENV = 'LIVE'
    
    call_count = {'count': 0}
    environments_checked = []
    
    # Track which environments are checked
    def mock_from_env(env):
        call_count['count'] += 1
        environments_checked.append(env)
        env_mock = MagicMock()
        env_mock.get.side_effect = lambda key, default=None: {
            'DOWNLOAD_FOLDER': None,
            'EQPATH': None,
            'SPECIAL_RESOURCES': {}
        }.get(key, default)
        return env_mock
    
    mock_settings.from_env = mock_from_env
    
    with patch('redfetch.config.initialize_config') as mock_init_config, \
         patch('redfetch.config.settings', mock_settings), \
         patch('redfetch.meta.config.settings', mock_settings), \
         patch('redfetch.auth.logout') as mock_logout, \
         patch('redfetch.meta.get_executable_path', return_value=None), \
         patch('redfetch.meta.detect_installation_method', return_value='pip'), \
         patch('redfetch.meta.Confirm.ask', return_value=False), \
         patch('redfetch.meta.write_commands_to_file') as mock_write_commands, \
         patch.dict(os.environ, {'REDFETCH_CONFIG_DIR': temp_config_dirs['config']}), \
         patch('sys.stdout', new_callable=StringIO):
        
        # Call uninstall - expect SystemExit(0) at the end
        with pytest.raises(SystemExit) as exc_info:
            meta.uninstall()
        
        # Verify it's a clean exit
        assert exc_info.value.code == 0
        
        # Verify that all 4 environments were checked
        assert call_count['count'] == 4
        assert 'DEFAULT' in environments_checked
        assert 'LIVE' in environments_checked
        assert 'TEST' in environments_checked
        assert 'EMU' in environments_checked
        
        # Verify that initialize_config was called
        mock_init_config.assert_called_once()
        
        # Verify that write_commands_to_file was called but prevented popup
        # (config dir will exist and be added to paths)
        assert mock_write_commands.called

