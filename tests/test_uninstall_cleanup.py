"""Tests for uninstall cleanup."""

import shlex
from pathlib import Path

import pytest

from redfetch import meta


class _FakeEnv:
    def __init__(self, download_folder=None, eqpath=None, special_resources=None):
        self._values = {
            "DOWNLOAD_FOLDER": download_folder,
            "EQPATH": eqpath,
            "SPECIAL_RESOURCES": special_resources or {},
        }

    def get(self, key, default=None):
        value = self._values.get(key)
        return default if value is None else value


class _FakeSettings:
    def __init__(self, envs):
        self._envs = envs

    def from_env(self, env):
        return self._envs.get(env, _FakeEnv())


@pytest.fixture
def install_settings(monkeypatch):
    monkeypatch.delenv("REDFETCH_CONFIG_DIR", raising=False)

    def _install(envs):
        monkeypatch.setattr(meta.config, "settings", _FakeSettings(envs))

    return _install


def test_nested_path_collapses_into_parent_regardless_of_env_order(tmp_path, install_settings):
    download = tmp_path / "dl"
    (download / "mq").mkdir(parents=True)
    install_settings({
        "DEFAULT": _FakeEnv(special_resources={"1974": {"custom_path": str(download / "mq")}}),
        "LIVE": _FakeEnv(download_folder=str(download)),
    })

    assert meta._collect_leftover_dirs() == {download}


def test_download_folder_shared_across_envs_reported_once(tmp_path, install_settings):
    download = tmp_path / "dl"
    download.mkdir()
    install_settings({
        "LIVE": _FakeEnv(download_folder=str(download)),
        "EMU": _FakeEnv(download_folder=str(download)),
    })

    assert meta._collect_leftover_dirs() == {download}


def test_eqpath_reports_only_its_maps_subdir(tmp_path, install_settings):
    eq = tmp_path / "EverQuest"
    (eq / "maps").mkdir(parents=True)
    install_settings({"LIVE": _FakeEnv(eqpath=str(eq))})

    assert meta._collect_leftover_dirs() == {eq / "maps"}


def test_nonexistent_dirs_are_dropped(tmp_path, install_settings):
    eq_without_maps = tmp_path / "EverQuest"
    eq_without_maps.mkdir()
    install_settings({
        "LIVE": _FakeEnv(
            download_folder=str(tmp_path / "never-created"),
            eqpath=str(eq_without_maps),
        )
    })

    assert meta._collect_leftover_dirs() == set()


def test_default_path_joins_download_folder_and_custom_path_stands_alone(tmp_path, install_settings):
    download = tmp_path / "dl"
    (download / "myseq").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    beside = tmp_path / "beside"
    beside.mkdir()
    install_settings({
        "LIVE": _FakeEnv(
            download_folder=str(download),
            special_resources={
                "153": {"default_path": "myseq"},
                "60": {"custom_path": str(elsewhere)},
                "303": {"default_path": "../beside"},  # normalized, not lexically pruned
            },
        )
    })

    assert meta._collect_leftover_dirs() == {download, elsewhere, beside}


def test_config_dir_is_reported_when_set(tmp_path, install_settings, monkeypatch):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    install_settings({})
    monkeypatch.setenv("REDFETCH_CONFIG_DIR", str(config_dir))

    assert meta._collect_leftover_dirs() == {config_dir}


def test_delete_config_files_takes_only_redfetch_files(tmp_path):
    (tmp_path / ".env").write_text("token", encoding="utf-8")
    (tmp_path / "settings.local.toml").write_text("[LIVE]", encoding="utf-8")
    (tmp_path / "legacy.db").touch()
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "blob").touch()
    (tmp_path / "unrelated.txt").write_text("keep me", encoding="utf-8")

    meta._delete_config_files(tmp_path)

    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "settings.local.toml").exists()
    assert not (tmp_path / "legacy.db").exists()
    assert not (tmp_path / ".cache").exists()
    assert (tmp_path / "unrelated.txt").exists()


def test_windows_commands_come_deepest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(meta.platform, "system", lambda: "Windows")
    parent = tmp_path / "rg"
    child = tmp_path / "rg" / "dl" / "mq"

    commands = meta.generate_removal_commands({parent, child})

    assert commands == [
        f"Remove-Item -LiteralPath '{child}' -Recurse -Force",
        f"Remove-Item -LiteralPath '{parent}' -Recurse -Force",
    ]


def test_windows_command_escapes_single_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(meta.platform, "system", lambda: "Windows")
    quirky = tmp_path / "it's here"

    commands = meta.generate_removal_commands({quirky})

    escaped = str(quirky).replace("'", "''")
    assert commands == [f"Remove-Item -LiteralPath '{escaped}' -Recurse -Force"]


def test_unix_command_escapes_single_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(meta.platform, "system", lambda: "Linux")
    quirky = tmp_path / "it's here"

    commands = meta.generate_removal_commands({quirky})

    # Assert what the shell would see, not the quoting style.
    assert [shlex.split(command) for command in commands] == [["rm", "-rf", str(quirky)]]


def test_batch_script_handles_percent_and_space_paths():
    script = meta._uninstall_batch_script(r"C:\100% mods\redfetch.exe", 4242)

    assert '"C:\\100%% mods\\redfetch.exe" self remove' in script
    assert 'del /f "C:\\100%% mods\\redfetch.exe"' in script
    assert '"PID eq 4242"' in script


def test_batch_script_switches_codepage_before_the_path_appears():
    script = meta._uninstall_batch_script("C:\\Éq\\redfetch.exe", 1)

    assert script.index("chcp 65001") < script.index("Éq")
    assert script.rstrip().endswith('(goto) 2>nul & del "%~f0"')


def test_commands_file_round_trips_exotic_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(meta.platform, "system", lambda: "Windows")
    monkeypatch.setattr(meta.Path, "home", lambda: tmp_path)
    opened = []
    monkeypatch.setattr(meta.os, "startfile", lambda p: opened.append(p), raising=False)

    exotic = tmp_path / "Éq—downloads"
    command = f"Remove-Item -LiteralPath '{exotic}' -Recurse -Force"

    meta.write_commands_to_file([command], {exotic})

    file_path = tmp_path / "redfetch_removal_commands.txt"
    assert opened == [file_path]
    content = file_path.read_text(encoding="utf-8-sig")
    assert f" - {exotic}" in content
    assert command in content
