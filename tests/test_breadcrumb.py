"""Tests for the last_command.json breadcrumb that lets MacroQuest discover redfetch."""
import os

import pytest

from redfetch import config


@pytest.fixture
def breadcrumb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_pyapp(monkeypatch):
    monkeypatch.delenv("PYAPP", raising=False)


def test_ignores_pyapp_pointing_at_wrong_binary(breadcrumb_dir, tmp_path, monkeypatch):
    hatch_exe = tmp_path / "hatch.exe"
    hatch_exe.write_text("")
    monkeypatch.setenv("PYAPP", str(hatch_exe))

    def fake_which(cmd):
        return None

    monkeypatch.setattr(config.shutil, "which", fake_which)

    config.write_breadcrumb()

    assert not (breadcrumb_dir / config.BREADCRUMB_FILENAME).exists()


def test_skips_write_when_only_sys_executable(breadcrumb_dir, monkeypatch):
    def fake_which(cmd):
        return None

    monkeypatch.setattr(config.shutil, "which", fake_which)

    config.write_breadcrumb()

    assert not (breadcrumb_dir / config.BREADCRUMB_FILENAME).exists()


def test_write_is_best_effort(breadcrumb_dir, monkeypatch):
    def fake_which(cmd):
        return "/usr/bin/redfetch"

    monkeypatch.setattr(config.shutil, "which", fake_which)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "makedirs", fail)
    config.write_breadcrumb()
