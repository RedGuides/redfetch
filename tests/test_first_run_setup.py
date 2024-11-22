import pytest
from unittest.mock import patch, MagicMock, mock_open
from redfetch.config_firstrun import first_run_setup

@pytest.fixture
def mock_user_config_dir():
    with patch('redfetch.config_firstrun.user_config_dir') as mock_dir:
        mock_dir.return_value = '/dummy/default_config_dir'
        yield mock_dir

@pytest.fixture
def mock_os_environ():
    with patch('redfetch.config_firstrun.os.environ', {'CI': 'false'}):
        yield

@pytest.fixture
def mock_console():
    with patch('redfetch.config_firstrun.console') as mock_console:
        yield mock_console

@pytest.fixture
def mock_prompt_ask():
    with patch('redfetch.config_firstrun.Prompt.ask') as mock_prompt:
        yield mock_prompt

@pytest.fixture
def mock_confirm_ask():
    with patch('redfetch.config_firstrun.Confirm.ask') as mock_confirm:
        yield mock_confirm

@pytest.fixture
def mock_os_path_exists():
    with patch('redfetch.config_firstrun.os.path.exists') as mock_exists:
        yield mock_exists

@pytest.fixture
def mock_os_makedirs():
    with patch('redfetch.config_firstrun.os.makedirs') as mock_makedirs:
        yield mock_makedirs

@pytest.fixture
def mock_create_first_run_flag():
    with patch('redfetch.config_firstrun.create_first_run_flag') as mock_flag:
        yield mock_flag

@pytest.fixture
def mock_open_file():
    with patch('builtins.open', mock_open(read_data='/dummy/config_dir')):
        yield

@pytest.fixture
def mock_platform_system():
    with patch('redfetch.config_firstrun.platform.system') as mock_system:
        # Mock to return 'Linux' to avoid Windows-specific code paths
        mock_system.return_value = 'Linux'
        yield mock_system

@pytest.fixture
def mock_custom_prompt_ask():
    with patch('redfetch.config_firstrun.CustomPrompt.ask') as mock_prompt:
        yield mock_prompt

def test_first_run_setup_first_time(
    mock_user_config_dir,
    mock_os_environ,
    mock_console,
    mock_prompt_ask,
    mock_custom_prompt_ask,
    mock_os_path_exists,
    mock_os_makedirs,
    mock_create_first_run_flag,
    mock_platform_system
):
    # Simulate first run (first_run_complete does not exist)
    mock_os_path_exists.return_value = False

    # Mock the CustomPrompt.ask() response for the wizard dialogue
    mock_custom_prompt_ask.return_value = "ready"
    
    # Simulate user selecting default configuration directory
    mock_prompt_ask.return_value = '1'

    config_dir = first_run_setup()

    # Assertions
    assert config_dir == '/dummy/default_config_dir'
    mock_os_makedirs.assert_called_with('/dummy/default_config_dir', exist_ok=True)
    mock_create_first_run_flag.assert_called_with('/dummy/default_config_dir', '/dummy/default_config_dir')

def test_first_run_setup_subsequent_run_without_env_file(
    mock_user_config_dir,
    mock_os_environ,
    mock_console,
    mock_os_path_exists,
    mock_prompt_ask,
    mock_custom_prompt_ask,
    mock_os_makedirs,
    mock_create_first_run_flag,
    mock_open_file,
    mock_platform_system
):
    # Create a side effect that returns specific values for the first two checks
    # and False for any additional checks
    def path_exists_side_effect(path):
        if '.first_run_complete' in str(path):
            return True
        if '.env' in str(path):
            return False
        return False  # Default response for any other path checks
    
    mock_os_path_exists.side_effect = path_exists_side_effect

    # Mock the CustomPrompt.ask() response for the wizard dialogue
    mock_custom_prompt_ask.return_value = "ready"
    
    # Simulate user selecting default configuration directory again
    mock_prompt_ask.return_value = '1'

    config_dir = first_run_setup()

    # Assertions
    assert config_dir == '/dummy/default_config_dir'
    mock_os_makedirs.assert_called_with('/dummy/default_config_dir', exist_ok=True)
    mock_create_first_run_flag.assert_called_with('/dummy/default_config_dir', '/dummy/default_config_dir')

def test_first_run_setup_ci_environment(
    mock_user_config_dir,
    mock_console,
    mock_os_makedirs,
    mock_create_first_run_flag,
    mock_platform_system
):
    # Simulate CI environment
    with patch('redfetch.config_firstrun.os.environ', {'CI': 'true'}):
        config_dir = first_run_setup()

    # Assertions
    assert config_dir == '/dummy/default_config_dir'
    mock_os_makedirs.assert_called_with('/dummy/default_config_dir', exist_ok=True)
    mock_create_first_run_flag.assert_called_with('/dummy/default_config_dir', '/dummy/default_config_dir')