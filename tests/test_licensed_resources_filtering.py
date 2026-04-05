"""Discovery-stage tests for licensed resource filtering."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from redfetch import sync_discovery as discovery
from redfetch import sync_planner as planner
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    LocalSnapshot,
    RemoteArtifact,
    RemoteResourceState,
    RemoteSnapshot,
)

FAR_FUTURE = int(time.time()) + 86400 * 365 * 10
PAST = int(time.time()) - 86400 * 30


def make_license(
    resource_id: int,
    parent_category_id: int,
    title: str = "Licensed Resource",
    *,
    version_id: int = 101,
    file_id: int = 1001,
    end_date: int = FAR_FUTURE,
) -> dict:
    return {
        "active": True,
        "start_date": 1711170000,
        "end_date": end_date,
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


# --- expired license discovery tests ---


def test_expired_license_gets_discovery_block():
    desired_set = asyncio.run(
        _discover_from_licenses([make_license(9998, 8, end_date=PAST)], "LIVE")
    )
    target = desired_set.install_targets["/9998/"]
    assert target.sources == {"licensed"}
    assert target.discovery_block == "license_expired"


def test_valid_license_has_no_discovery_block():
    desired_set = asyncio.run(
        _discover_from_licenses([make_license(9998, 8, end_date=FAR_FUTURE)], "LIVE")
    )
    target = desired_set.install_targets["/9998/"]
    assert target.sources == {"licensed"}
    assert target.discovery_block is None


def test_unlimited_license_has_no_discovery_block():
    desired_set = asyncio.run(
        _discover_from_licenses([make_license(9998, 8, end_date=0)], "LIVE")
    )
    target = desired_set.install_targets["/9998/"]
    assert target.sources == {"licensed"}
    assert target.discovery_block is None


# --- expired license planner tests ---


def _downloadable_state(resource_id: str, *, category_id: int = 8) -> RemoteResourceState:
    return RemoteResourceState(
        resource_id=resource_id,
        title=f"Resource {resource_id}",
        category_id=category_id,
        version_id=1234,
        status="downloadable",
        artifact=RemoteArtifact(
            file_id=9876,
            filename=f"{resource_id}.zip",
            download_url=f"https://example.com/{resource_id}.zip",
            file_hash="d41d8cd98f00b204e9800998ecf8427e",
        ),
        source_note="manifest_plus_access_check",
    )


def _build_plan_for_target(target, remote_state):
    mock_settings = MagicMock()
    mock_settings.ENV = "LIVE"
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER="C:/downloads",
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
    )
    desired_set = DesiredSet(
        mode="full",
        resource_ids={target.resource_id},
        install_targets={target.target_key: target},
    )
    with patch("redfetch.sync_planner.config.settings", mock_settings), \
         patch("redfetch.sync_planner.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}):
        return planner.build_execution_plan(
            desired_set=desired_set,
            remote_snapshot=RemoteSnapshot(resources={target.resource_id: remote_state}),
            local_snapshot=LocalSnapshot(),
            settings_env="LIVE",
        )


def test_planner_blocks_on_discovery_block():
    target = DesiredInstallTarget(
        target_key="/9998/",
        resource_id="9998",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="9998",
        target_kind="root",
        sources={"licensed"},
        title="Expired Resource",
        category_id=8,
        resolved_path="C:/downloads/9998",
        discovery_block="license_expired",
    )
    plan = _build_plan_for_target(target, _downloadable_state("9998"))
    action = plan.actions["/9998/"]
    assert action.action == "block"
    assert action.reason == "license_expired"


def test_planner_blocks_discovery_block_even_when_watching():
    target = DesiredInstallTarget(
        target_key="/9998/",
        resource_id="9998",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="9998",
        target_kind="root",
        sources={"watching", "licensed"},
        title="Watched + Expired Resource",
        category_id=8,
        resolved_path="C:/downloads/9998",
        discovery_block="license_expired",
    )
    plan = _build_plan_for_target(target, _downloadable_state("9998"))
    action = plan.actions["/9998/"]
    assert action.action == "block"
    assert action.reason == "license_expired"
