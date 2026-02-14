import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from redfetch import config, store, main


def _set_env_settings(download_folder: str, eqpath: str, special_resources: dict) -> None:
    env_settings = SimpleNamespace(
        DOWNLOAD_FOLDER=download_folder,
        EQPATH=eqpath,
        SPECIAL_RESOURCES=special_resources,
    )
    config.settings = MagicMock()
    config.settings.ENV = "LIVE"
    config.settings.from_env.return_value = env_settings


def test_reconcile_global_reset_on_base_path_change(tmp_path):
    config.config_dir = str(tmp_path)
    db_name = "LIVE_resources.db"

    download_folder_1 = str(tmp_path / "downloads1")
    _set_env_settings(download_folder_1, "", {})
    store.initialize_db(db_name, "LIVE")

    with store.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO downloads (resource_id, parent_id, version_local) VALUES (?, ?, ?)",
            (153, 0, 5),
        )

    download_folder_2 = str(tmp_path / "downloads2")
    _set_env_settings(download_folder_2, "", {})
    store.initialize_db(db_name, "LIVE")

    with store.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT version_local FROM downloads WHERE resource_id = ? AND parent_id = 0",
            (153,),
        )
        row = cursor.fetchone()
    assert row[0] == 0


def test_reconcile_targeted_reset_on_special_destination_change(tmp_path):
    config.config_dir = str(tmp_path)
    db_name = "LIVE_resources.db"

    download_folder = str(tmp_path / "downloads")
    eqpath_1 = str(tmp_path / "eq1")
    eqpath_2 = str(tmp_path / "eq2")

    special_resources_1 = {
        "153": {"default_path": os.path.join(eqpath_1, "maps"), "custom_path": ""},
        "1865": {"default_path": "MySEQ", "custom_path": ""},
    }
    _set_env_settings(download_folder, eqpath_1, special_resources_1)
    store.initialize_db(db_name, "LIVE")

    with store.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO downloads (resource_id, parent_id, version_local) VALUES (?, ?, ?)",
            (153, 0, 9),
        )
        cursor.execute(
            "INSERT INTO downloads (resource_id, parent_id, version_local) VALUES (?, ?, ?)",
            (999, 153, 7),
        )
        cursor.execute(
            "INSERT INTO downloads (resource_id, parent_id, version_local) VALUES (?, ?, ?)",
            (1865, 0, 8),
        )
        cursor.execute(
            "INSERT INTO downloads (resource_id, parent_id, version_local) VALUES (?, ?, ?)",
            (153, 151, 6),
        )

    special_resources_2 = {
        "153": {"default_path": os.path.join(eqpath_2, "maps"), "custom_path": ""},
        "1865": {"default_path": "MySEQ", "custom_path": ""},
    }
    _set_env_settings(download_folder, eqpath_2, special_resources_2)
    store.initialize_db(db_name, "LIVE")

    with store.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT version_local FROM downloads WHERE resource_id = ? AND parent_id = 0",
            (153,),
        )
        row_153_root = cursor.fetchone()
        cursor.execute(
            "SELECT version_local FROM downloads WHERE resource_id = ? AND parent_id = ?",
            (999, 153),
        )
        row_153_dep = cursor.fetchone()
        cursor.execute(
            "SELECT version_local FROM downloads WHERE resource_id = ? AND parent_id = 0",
            (1865,),
        )
        row_1865_root = cursor.fetchone()
        cursor.execute(
            "SELECT version_local FROM downloads WHERE resource_id = ? AND parent_id = ?",
            (153, 151),
        )
        row_153_as_dep = cursor.fetchone()

    assert row_153_root[0] == 0
    assert row_153_dep[0] == 0
    assert row_1865_root[0] == 8
    assert row_153_as_dep[0] == 6


def test_config_command_reconciles_signature_for_server(monkeypatch):
    called = {}

    def fake_init():
        return None

    def fake_update(setting_path, value, env=None):
        return None

    def fake_reconcile(db_name, settings_env):
        called["db_name"] = db_name
        called["settings_env"] = settings_env
        return None

    monkeypatch.setattr(config, "initialize_config", fake_init)
    monkeypatch.setattr(config, "update_setting", fake_update)
    monkeypatch.setattr(store, "reconcile_install_signature", fake_reconcile)

    main.config_command("EQPATH", "C:\\Games\\EverQuest", server=main.Env.TEST)

    assert called == {"db_name": "TEST_resources.db", "settings_env": "TEST"}
