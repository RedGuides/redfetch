"""Cancellation tests for the staged sync pipeline."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from redfetch import sync
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    ExecutionPlan,
    LocalSnapshot,
    PlannedAction,
)


def _minimal_desired_set() -> DesiredSet:
    target = DesiredInstallTarget(
        target_key="/2001/",
        resource_id="2001",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="2001",
        target_kind="root",
        sources={"watching"},
        title="Res C",
        category_id=11,
        resolved_path="C:/downloads/2001",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=False,
    )
    return DesiredSet(
        mode="full",
        resource_ids={"2001"},
        install_targets={target.target_key: target},
    )


def _minimal_plan() -> ExecutionPlan:
    return ExecutionPlan(
        actions={
            "/2001/": PlannedAction(
                target_key="/2001/",
                resource_id="2001",
                parent_id=None,
                parent_target_key=None,
                root_resource_id="2001",
                target_kind="root",
                action="download",
                reason="not_installed",
                title="Res C",
                category_id=11,
                remote_version=2001,
                artifact=None,
                resolved_path="C:/downloads/2001",
                subfolder=None,
                flatten=False,
                protected_files=[],
                explicit_root=False,
            )
        },
    )


def _mock_settings():
    mock_settings = MagicMock()
    mock_settings.ENV = "LIVE"
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER="C:\\downloads",
        EQPATH="",
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
    )
    return mock_settings


def test_cancel_before_stage_build_returns_false(tmp_path):
    db_path = str(tmp_path / "cancel.db")
    with patch(
        "redfetch.sync.config.settings",
        _mock_settings(),
    ), patch(
        "redfetch.sync.store.load_local_snapshot",
        new=AsyncMock(return_value=LocalSnapshot()),
    ), patch(
        "redfetch.sync.sync_discovery.discover_desired_set",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ), patch(
        "redfetch.sync.sync_executor.execute_plan",
        new=AsyncMock(),
    ) as execute_mock:
        ok = asyncio.run(sync.run_sync(db_path, headers={}, resource_ids=None))

    assert ok is False
    assert execute_mock.await_count == 0


def test_cancel_during_execution_returns_false(tmp_path):
    db_path = str(tmp_path / "cancel.db")
    with patch(
        "redfetch.sync.config.settings",
        _mock_settings(),
    ), patch(
        "redfetch.sync.store.load_local_snapshot",
        new=AsyncMock(return_value=LocalSnapshot()),
    ), patch(
        "redfetch.sync.sync_discovery.discover_desired_set",
        new=AsyncMock(return_value=_minimal_desired_set()),
    ), patch(
        "redfetch.sync.sync_remote.fetch_remote_snapshot",
        new=AsyncMock(return_value=None),
    ), patch(
        "redfetch.sync.sync_planner.build_execution_plan",
        return_value=_minimal_plan(),
    ), patch(
        "redfetch.sync.sync_executor.execute_plan",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        ok = asyncio.run(sync.run_sync(db_path, headers={}, resource_ids=None))

    assert ok is False
