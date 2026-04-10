"""Tests for environment selection via --server."""
from types import SimpleNamespace

import pytest

from redfetch.main import Env, _apply_server_override
from redfetch import config


@pytest.fixture
def fake_config(monkeypatch):
    """Patch config.settings with a fake and capture switch_environment calls."""
    fake = SimpleNamespace(ENV="LIVE")
    monkeypatch.setattr(config, "settings", fake)
    switched = []

    def fake_switch(new_env):
        fake.ENV = new_env
        switched.append(new_env)

    monkeypatch.setattr(config, "switch_environment", fake_switch)
    return fake, switched


def test_server_flag_switches_environment(fake_config):
    """--server EMU while on LIVE must trigger a persistent switch to EMU."""
    settings, switched = fake_config
    _apply_server_override(server=Env.EMU)
    assert switched == ["EMU"]
    assert settings.ENV == "EMU"
