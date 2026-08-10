"""Helpers shared across the test suite."""
import os

from dynaconf import Dynaconf

from redfetch import config


def _install_settings(tmp_path, monkeypatch, local_toml="", bundle_toml=None, env="EMU",
                      current_env=None):
    """Install a real Dynaconf instance backed by temporary settings files.

    A fixture bundle also swaps config._base_settings_cache, so mutator tests
    prune/validate against the fixture rather than the real bundle.
    """
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    if current_env:
        monkeypatch.setenv("REDFETCH_ENV", current_env)
    if bundle_toml is None:
        bundle = os.path.join(config.script_dir, "settings.toml")
    else:
        bundle_file = tmp_path / "bundle.toml"
        bundle_file.write_text(bundle_toml, encoding="utf-8")
        bundle = str(bundle_file)
        fixture_base = Dynaconf(
            settings_files=[bundle],
            environments=True,
            merge_enabled=True,
            env_switcher="REDFETCH_ENV",
        )
        monkeypatch.setattr(config, "_base_settings_cache", fixture_base)
    local = tmp_path / "settings.local.toml"
    local.write_text(local_toml, encoding="utf-8")
    real = Dynaconf(
        settings_files=[bundle, str(local)],
        environments=True,
        merge_enabled=True,
        env_switcher="REDFETCH_ENV",
    )
    real.ENV = env
    monkeypatch.setattr(config, "settings", real)
    monkeypatch.setattr(config, "config_dir", str(tmp_path))
    return real


class FakeInput:
    def __init__(self, value=""):
        self.value = value


def _eq_folder(tmp_path, name="EQ"):
    """A folder the real eqgame.exe gates accept."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "eqgame.exe").write_bytes(b"MZ")
    return folder
