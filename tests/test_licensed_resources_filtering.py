"""Discovery-stage tests for licensed resource filtering."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from redfetch import sync_discovery as discovery


def make_license(
    resource_id: int,
    parent_category_id: int,
    title: str = "Licensed Resource",
    *,
    version_id: int = 101,
    file_id: int = 1001,
) -> dict:
    return {
        "active": True,
        "start_date": "2024-01-01",
        "end_date": "2025-01-01",
        "license_id": 12345,
        "resource": {
            "resource_id": resource_id,
            "title": title,
            "Category": {"parent_category_id": parent_category_id},
            "current_files": [
                {
                    "id": file_id,
                    "filename": "package.zip",
                    "download_url": "https://example.com/file.zip",
                    "hash": "d41d8cd98f00b204e9800998ecf8427e",
                }
            ],
            "current_version": {"version_id": version_id},
        },
    }


async def _discover_from_licenses(licenses: list[dict], env: str):
    mock_settings = MagicMock()
    mock_settings.ENV = env
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER="C:\\downloads",
        EQPATH="",
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
    )

    with patch(
        "redfetch.sync_discovery.api.fetch_watched_resources",
        new=AsyncMock(return_value=[]),
    ), patch(
        "redfetch.sync_discovery.api.fetch_licenses",
        new=AsyncMock(return_value=licenses),
    ), patch(
        "redfetch.sync_discovery.config.settings",
        mock_settings,
    ), patch(
        "redfetch.sync_discovery.config.CATEGORY_MAP",
        {8: "macros", 11: "plugins", 25: "lua"},
    ):
        async with httpx.AsyncClient() as client:
            return await discovery.discover_desired_set(
                client=client,
                resource_ids=None,
                settings_env=env,
            )


@pytest.mark.parametrize(
    "env,expected_target",
    [
        ("TEST", False),
        ("EMU", False),
        ("LIVE", True),
    ],
)
def test_licensed_plugins_only_live(env, expected_target):
    desired_set = asyncio.run(_discover_from_licenses([make_license(9999, 11)], env))
    target_key = "/9999/"
    assert (target_key in desired_set.install_targets) is expected_target
    if expected_target:
        assert desired_set.install_targets[target_key].sources == {"licensed"}


@pytest.mark.parametrize(
    "env,category_id,resource_id",
    [
        ("LIVE", 8, 9998),
        ("TEST", 8, 9998),
        ("EMU", 8, 9998),
        ("LIVE", 25, 9997),
        ("TEST", 25, 9997),
        ("EMU", 25, 9997),
    ],
)
def test_cross_compatible_licensed_resources_remain_in_scope(env, category_id, resource_id):
    desired_set = asyncio.run(
        _discover_from_licenses(
            [make_license(resource_id, category_id, "Cross Compatible")],
            env,
        )
    )

    target_key = f"/{resource_id}/"
    assert target_key in desired_set.install_targets
    assert desired_set.install_targets[target_key].sources == {"licensed"}
