"""MQ2EasyFind on emu: the one yaml key redfetch flips, and only on the RoF2 client."""
import pytest
import yaml

from conftest import _install_settings
from redfetch import sync, utils

# Laid out the way the plugin's yaml-cpp emitter writes it (indent 4).
PLUGIN_FILE = """GlobalLogLevel: info
Colors:
    AddedLocation: 4278255360
DisabledTransferTypes:
    - Gate
    - Translocator
ColoredFindWindow: true
IgnoreZoneConnectionData: false
"""


def _mq(tmp_path, monkeypatch, yaml_text=None):
    mq = tmp_path / "VanillaMQ_EMU"
    (mq / "config").mkdir(parents=True)
    if yaml_text is not None:
        (mq / "config" / "EasyFind.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(utils, "get_vvmq_path", lambda: str(mq))
    return mq


def _load(mq):
    with open(mq / "config" / "EasyFind.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_creates_file_when_plugin_has_not_written_one(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = _mq(tmp_path, monkeypatch)
    sync.force_easyfind_emu_mode()
    assert _load(mq) == {"IgnoreZoneConnectionData": True}


def test_flips_false_and_keeps_every_other_setting(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = _mq(tmp_path, monkeypatch, PLUGIN_FILE)
    sync.force_easyfind_emu_mode()
    expected = yaml.safe_load(PLUGIN_FILE)
    expected["IgnoreZoneConnectionData"] = True
    assert list(_load(mq).items()) == list(expected.items())  # values and order


def test_adds_key_when_missing(tmp_path, monkeypatch):
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = _mq(tmp_path, monkeypatch, "GlobalLogLevel: info\n")
    sync.force_easyfind_emu_mode()
    assert _load(mq) == {"GlobalLogLevel": "info", "IgnoreZoneConnectionData": True}


def test_already_true_is_untouched(tmp_path, monkeypatch, capsys):
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = _mq(tmp_path, monkeypatch, "IgnoreZoneConnectionData: true\nGlobalLogLevel: info\n")
    sync.force_easyfind_emu_mode()
    # not rewritten: the plugin's own formatting survives
    assert (mq / "config" / "EasyFind.yaml").read_text(encoding="utf-8") == (
        "IgnoreZoneConnectionData: true\nGlobalLogLevel: info\n"
    )
    assert "EasyFind" not in capsys.readouterr().out


@pytest.mark.parametrize("junk", ["", "just a string\n"])
def test_empty_or_junk_file_is_replaced(tmp_path, monkeypatch, junk):
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = _mq(tmp_path, monkeypatch, junk)
    sync.force_easyfind_emu_mode()
    assert _load(mq) == {"IgnoreZoneConnectionData": True}


@pytest.mark.parametrize("env", ["LIVE", "TEST"])
def test_leaves_live_and_test_alone(tmp_path, monkeypatch, env):
    """Live EasyFind needs its zone connection data."""
    _install_settings(tmp_path, monkeypatch, env=env)
    mq = _mq(tmp_path, monkeypatch, "IgnoreZoneConnectionData: false\n")
    sync.force_easyfind_emu_mode()
    assert _load(mq) == {"IgnoreZoneConnectionData": False}


def test_skips_a_folder_without_config_dir(tmp_path, monkeypatch):
    """Don't seed config/ into a folder MacroQuest hasn't populated yet."""
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = tmp_path / "not-yet-mq"
    mq.mkdir()
    monkeypatch.setattr(utils, "get_vvmq_path", lambda: str(mq))
    sync.force_easyfind_emu_mode()
    assert not (mq / "config").exists()


def test_never_raises(tmp_path, monkeypatch, capsys):
    """A malformed file is a warning, not a failed sync."""
    _install_settings(tmp_path, monkeypatch, env="EMU")
    mq = _mq(tmp_path, monkeypatch, "IgnoreZoneConnectionData: [unclosed\n")
    sync.force_easyfind_emu_mode()
    assert "warning" in capsys.readouterr().out
    assert (mq / "config" / "EasyFind.yaml").read_text(encoding="utf-8") == "IgnoreZoneConnectionData: [unclosed\n"
