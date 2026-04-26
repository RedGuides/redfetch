import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from redfetch import config


class _SettingsStub:
    ENV = "LIVE"

    def __init__(self, values):
        self._values = values

    def from_env(self, env):
        return self._values


def test_main_auto_run_eqbcs_if_enabled(monkeypatch):
    monkeypatch.setattr(config, "settings", _SettingsStub({"AUTO_RUN_EQBCS": True}), raising=False)
    main = importlib.import_module("redfetch.main")

    with patch("redfetch.main.utils.get_vvmq_path", return_value="C:/MQ"), \
         patch("redfetch.main.processes.run_executable") as run_mock, \
         patch("redfetch.main.sys.platform", "win32"), \
         patch.dict("redfetch.main.os.environ", {}, clear=True):
        main.auto_run_eqbcs_if_enabled()

    run_mock.assert_called_once_with("C:/MQ", "EQBCS.exe")


def test_terminal_ui_auto_run_eqbcs_if_enabled(monkeypatch):
    monkeypatch.setattr(config, "settings", _SettingsStub({"AUTO_RUN_EQBCS": True}), raising=False)
    Redfetch = importlib.import_module("redfetch.terminal_ui").Redfetch

    app = SimpleNamespace(current_env="LIVE", run_executable=MagicMock())

    with patch("redfetch.terminal_ui.utils.get_vvmq_path", return_value="C:/MQ"), \
         patch("redfetch.terminal_ui.sys.platform", "win32"):
        Redfetch.auto_run_eqbcs_if_enabled(app)

    app.run_executable.assert_called_once_with("C:/MQ", "EQBCS.exe")
