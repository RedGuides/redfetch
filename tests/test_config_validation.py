from dynaconf import ValidationError
import pytest

from redfetch.config import normalize_paths_in_dict


def test_special_resource_relative_path_skips_eqgame_validation():
    data = {
        "1974": {
            "default_path": "VanillaMQ_LIVE",
        }
    }

    normalized = normalize_paths_in_dict(data)

    assert normalized["1974"]["default_path"] == "VanillaMQ_LIVE"


def test_special_resource_absolute_path_still_rejects_eqgame_parent(tmp_path):
    eq_root = tmp_path / "EverQuest"
    vv_path = eq_root / "VanillaMQ_LIVE"
    eq_root.mkdir()
    vv_path.mkdir()
    (eq_root / "eqgame.exe").write_text("", encoding="utf-8")

    data = {
        "1974": {
            "default_path": str(vv_path),
        }
    }

    with pytest.raises(ValidationError, match=r"contains eqgame\.exe"):
        normalize_paths_in_dict(data)
