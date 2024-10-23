import pytest
from unittest.mock import patch, MagicMock
from redfetch import utils, config

# Realistic and complex sample data to mock config.settings.SPECIAL_RESOURCES
special_resources_mock = {
    '1974': {'opt_in': True, 'dependencies': {}},
    '151': {
        'opt_in': True,
        'dependencies': {
            '153': {'subfolder': 'maps', 'flatten': True, 'opt_in': True},
            '1865': {'subfolder': '', 'flatten': False, 'opt_in': True}
        }
    },
    '153': {
        'opt_in': True,
        'dependencies': {
            '151': {'subfolder': 'Flan', 'flatten': False, 'opt_in': True}
        }
    },
    '303': {
        'opt_in': True,
        'dependencies': {
            '151': {'subfolder': 'Flan2', 'flatten': False, 'opt_in': True},
            '3032': {'subfolder': '', 'flatten': False, 'opt_in': False}
        }
    },
    '1865': {
        'opt_in': False,  # Not opted in as special but is a dependency
        'dependencies': {}
    }
}

@pytest.fixture(autouse=True)
def mock_first_run_setup(mocker):
    # Mock the first_run_setup function to return a dummy config dir
    mock_setup = mocker.patch('redfetch.config.first_run_setup')
    mock_setup.return_value = '/dummy/config/dir'
    return mock_setup

@pytest.fixture
def mock_config(mocker):
    # Create a mock for the config.settings object
    mock_settings = MagicMock()
    
    # Create a mock for the object returned by from_env
    mock_env_settings = MagicMock()
    mock_env_settings.SPECIAL_RESOURCES = special_resources_mock
    
    # Set up the from_env method to return our mock_env_settings
    mock_settings.from_env.return_value = mock_env_settings
    
    # Patch config.settings
    mocker.patch('redfetch.config.settings', mock_settings)

    # If ENV is used in your code, you might want to set it as well
    mock_settings.ENV = 'test'

    return mock_settings

def test_is_special_or_dependency_special(mock_config):
    # Test to check if the function correctly identifies a special resource
    is_special, is_dependency, parent_ids = utils.is_special_or_dependency('1974')
    assert is_special == True
    assert is_dependency == False
    assert parent_ids == []

def test_is_special_or_dependency_dependency(mock_config):
    # Test to check if the function correctly identifies a resource that is both special and a dependency
    is_special, is_dependency, parent_ids = utils.is_special_or_dependency('153')
    assert is_special == True
    assert is_dependency == True
    assert parent_ids == ['151']

def test_is_special_or_dependency_neither(mock_config):
    # Test to check if the function correctly identifies a resource that is neither special nor a dependency
    is_special, is_dependency, parent_ids = utils.is_special_or_dependency('3032')
    assert is_special == False
    assert is_dependency == False
    assert parent_ids == []

def test_is_special_or_dependency_false_true(mock_config):
    # Test to check if the function correctly identifies a resource that is not special but is a dependency
    is_special, is_dependency, parent_ids = utils.is_special_or_dependency('1865')
    assert is_special == False
    assert is_dependency == True
    assert parent_ids == ['151']
