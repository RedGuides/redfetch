import os

import pytest
from unittest.mock import patch
from redfetch.config_firstrun import (
    _shape_problem,
    _write_problem,
    create_first_run_flag,
    first_run_setup,
    is_configured,
    setup_directories,
)

@pytest.fixture
def default_config_dir(tmp_path):
    """A real directory, because setup now proves it can create a file before accepting it."""
    path = tmp_path / "default_config_dir"
    path.mkdir()
    return str(path)

@pytest.fixture
def mock_user_config_dir(default_config_dir):
    with patch('redfetch.config_firstrun.user_config_dir') as mock_dir:
        mock_dir.return_value = default_config_dir
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
    default_config_dir,
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
    assert config_dir == default_config_dir
    mock_os_makedirs.assert_called_with(default_config_dir, exist_ok=True)
    mock_create_first_run_flag.assert_called_with(default_config_dir, default_config_dir)

def test_is_configured_false_when_no_flag(tmp_path):
    """No first_run_complete file at all -> not configured."""
    assert is_configured(str(tmp_path)) is False


def test_is_configured_false_when_flag_but_no_env(tmp_path):
    """Flag points at a real dir, but there's no .env -> not configured."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (tmp_path / "first_run_complete").write_text(str(config_dir))
    assert is_configured(str(tmp_path)) is False


def test_is_configured_true_when_flag_and_env_present(tmp_path):
    """Flag points at a dir that contains a .env -> configured."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").touch()
    (tmp_path / "first_run_complete").write_text(str(config_dir))
    assert is_configured(str(tmp_path)) is True


def test_flag_round_trips_non_ascii_config_dir(tmp_path):
    config_dir = tmp_path / "cönfig-Δir"
    config_dir.mkdir()
    (config_dir / ".env").touch()
    create_first_run_flag(str(tmp_path), str(config_dir))
    assert is_configured(str(tmp_path)) is True


def test_is_configured_tolerates_legacy_locale_encoded_flag(tmp_path):
    legacy_path = str(tmp_path / "café")
    (tmp_path / "first_run_complete").write_bytes(legacy_path.encode("cp1252"))
    assert is_configured(str(tmp_path)) is False


def test_shape_problem_accepts_a_normal_directory(tmp_path):
    assert _shape_problem(str(tmp_path)) is None


@pytest.mark.parametrize("answer", [".", "redfetch-config"])
def test_shape_problem_rejects_relative_path(answer):
    """A blank answer normpaths to '.', which would follow the working directory around."""
    assert "not a full path" in _shape_problem(answer)


def test_shape_problem_rejects_drive_root():
    """The reported crash: C:\\ takes files from an elevated run but denies a normal one."""
    assert "root of a drive" in _shape_problem(os.path.abspath(os.sep))


def test_write_problem_accepts_a_normal_directory(tmp_path):
    assert _write_problem(str(tmp_path)) is None


def test_write_problem_reports_a_missing_directory(tmp_path):
    assert "not a directory" in _write_problem(str(tmp_path / "gone"))


def test_write_problem_reports_a_read_only_directory(tmp_path):
    """os.access() passes on C:\\, so the probe has to try a real file create."""
    with patch(
        "redfetch.config_firstrun.tempfile.NamedTemporaryFile",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        problem = _write_problem(str(tmp_path))
    assert "can't create files" in problem


def test_setup_directories_refuses_a_drive_root_and_reprompts(
    tmp_path, mock_user_config_dir, mock_console, mock_platform_system
):
    """Typing C:\\ at the custom prompt loops back instead of being saved."""
    good_dir = tmp_path / "redfetch"
    good_dir.mkdir()

    # Linux layout: "2" is Custom Directory. The "" answers are "Press Enter to continue".
    answers = ["2", os.path.abspath(os.sep), "", "2", str(good_dir)]
    with patch("redfetch.config_firstrun.Prompt.ask", side_effect=answers):
        chosen = setup_directories()

    assert chosen == str(good_dir)


def test_setup_directories_survives_many_bad_answers(
    tmp_path, mock_user_config_dir, mock_console, mock_platform_system
):
    """Refusals loop instead of recursing, so a stubborn answer can't trip RecursionError."""
    good_dir = tmp_path / "eventually"
    good_dir.mkdir()

    # Deep enough to blow the default recursion limit if this ever recursed again.
    answers = ["2", os.path.abspath(os.sep), ""] * 800 + ["2", str(good_dir)]
    with patch("redfetch.config_firstrun.Prompt.ask", side_effect=answers):
        assert setup_directories() == str(good_dir)


def test_first_run_setup_reruns_when_saved_dir_is_a_drive_root(
    tmp_path, mock_console, mock_platform_system
):
    """Regression for the C:\\ report: reprompt rather than crash on every launch."""
    default_config_dir = tmp_path / "default"
    default_config_dir.mkdir()
    drive_root = os.path.abspath(os.sep)
    (default_config_dir / "first_run_complete").write_text(drive_root, encoding="utf-8")

    with (
        patch("redfetch.config_firstrun.user_config_dir", return_value=str(default_config_dir)),
        patch("redfetch.config_firstrun.os.environ", {"CI": "false"}),
        patch("redfetch.config_firstrun._greet_wizards"),
        patch("redfetch.config_firstrun.find_everquest_uninstall_location", return_value=None),
        patch("redfetch.config_firstrun.get_rg_utility_paths", return_value={}),
        patch("redfetch.config_firstrun.Prompt.ask", return_value="1"),
    ):
        config_dir = first_run_setup()

    # Reran the picker and took the OS config dir instead of the drive root.
    assert config_dir == str(default_config_dir)
    assert config_dir != drive_root


def test_first_run_setup_ci_environment(
    default_config_dir,
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
    assert config_dir == default_config_dir
    mock_os_makedirs.assert_called_with(default_config_dir, exist_ok=True)
    mock_create_first_run_flag.assert_called_with(default_config_dir, default_config_dir)