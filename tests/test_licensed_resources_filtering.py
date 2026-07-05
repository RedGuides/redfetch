"""Discovery-stage tests: watched/licensed roots sourced from rgsync, hydrated from the manifest."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from redfetch import sync_discovery as discovery
from redfetch.sync_types import SyncInfo

HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _manifest_entry(parent_category_id, *, title="Licensed Resource", version_id=101):
    return {
        "version_id": version_id,
        "title": title,
        "parent_category_id": parent_category_id,
        "access_tier": "public",
        "requires_license": False,
        "current_files": [
            {"id": 1001, "filename": "package.zip", "download_url": "https://example.com/file.zip", "hash": HASH}
        ],
    }


def _discover(sync_info: SyncInfo, manifest_entries: dict, env: str):
    mock_settings = MagicMock()
    mock_settings.ENV = env
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER="C:\\downloads",
        EQPATH="",
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
    )
    with patch("redfetch.sync_discovery.config.settings", mock_settings), \
         patch("redfetch.sync_discovery.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}):
        return discovery.discover_desired_set(
            resource_ids=None,
            sync_info=sync_info,
            manifest={"resources": manifest_entries},
            settings_env=env,
        )


@pytest.mark.parametrize(
    "env,category_id,in_scope",
    [
        # plugins (cat 11) are gated out of TEST/EMU; other mapped categories stay in scope everywhere.
        ("LIVE", 11, True), ("TEST", 11, False), ("EMU", 11, False),
        ("LIVE", 8, True), ("TEST", 8, True), ("EMU", 8, True),
    ],
)
def test_category_env_gating(env, category_id, in_scope):
    desired_set = _discover(SyncInfo(licensed_ids={"9999"}), {"9999": _manifest_entry(category_id)}, env)
    assert ("/9999/" in desired_set.install_targets) is in_scope
    if in_scope:
        assert desired_set.install_targets["/9999/"].sources == {"licensed"}


def test_watched_resource_hydrates_title_and_category_from_manifest():
    desired_set = _discover(SyncInfo(watched={"2"}), {"2": _manifest_entry(8, title="Guidestone.mac")}, "LIVE")
    target = desired_set.install_targets["/2/"]
    assert target.sources == {"watching"}
    assert target.title == "Guidestone.mac"
    assert target.category_id == 8


def test_watched_and_licensed_merge_into_one_target():
    desired_set = _discover(
        SyncInfo(watched={"9998"}, licensed_ids={"9998"}),
        {"9998": _manifest_entry(8, title="Both")},
        "LIVE",
    )
    target = desired_set.install_targets["/9998/"]
    assert target.sources == {"watching", "licensed"}
    assert target.title == "Both"


def test_resource_absent_from_manifest_is_skipped():
    """rgsync sends ids only; an id with no manifest entry has no resolvable category, so it's dropped."""
    desired_set = _discover(SyncInfo(watched={"404404"}), {}, "LIVE")
    assert desired_set.install_targets == {}
