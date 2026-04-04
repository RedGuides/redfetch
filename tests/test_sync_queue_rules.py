"""Planner-level tests for execution eligibility and blocking."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from redfetch import sync
from redfetch.sync_planner import build_execution_plan
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    ExecutionResult,
    ExecutionResultItem,
    LocalInstallState,
    LocalSnapshot,
    RemoteArtifact,
    RemoteResourceState,
    RemoteSnapshot,
)


def root_target(resource_id: str, *, title: str = "Resource", category_id: int = 8) -> DesiredInstallTarget:
    return DesiredInstallTarget(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        parent_id=None,
        parent_target_key=None,
        root_resource_id=resource_id,
        target_kind="root",
        sources={"special"},
        title=title,
        category_id=category_id,
        resolved_path=f"C:/downloads/{resource_id}",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=False,
    )


def dependency_target(resource_id: str, parent_id: str) -> DesiredInstallTarget:
    return DesiredInstallTarget(
        target_key=f"/{parent_id}/{resource_id}/",
        resource_id=resource_id,
        parent_id=parent_id,
        parent_target_key=f"/{parent_id}/",
        root_resource_id=parent_id,
        target_kind="dependency",
        sources={"dependency"},
        title=f"Dependency {resource_id}",
        category_id=8,
        resolved_path=f"C:/downloads/{parent_id}/{resource_id}",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=False,
    )


def desired_set_with_targets(*targets: DesiredInstallTarget) -> DesiredSet:
    return DesiredSet(
        mode="full",
        resource_ids={target.resource_id for target in targets},
        install_targets={target.target_key: target for target in targets},
    )


def downloadable_remote(resource_id: str, *, version_id: int = 101, title: str = "Resource") -> RemoteResourceState:
    return RemoteResourceState(
        resource_id=resource_id,
        title=title,
        category_id=8,
        version_id=version_id,
        status="downloadable",
        artifact=RemoteArtifact(
            file_id=version_id * 10,
            filename=f"{resource_id}.zip",
            download_url=f"https://example.com/{resource_id}.zip",
            file_hash="d41d8cd98f00b204e9800998ecf8427e",
        ),
        source_note="manifest_plus_access_check",
    )


def blocked_remote(resource_id: str, status: str) -> RemoteResourceState:
    return RemoteResourceState(
        resource_id=resource_id,
        title=f"Blocked {resource_id}",
        category_id=8,
        version_id=101,
        status=status,
        artifact=None,
        source_note="api_fallback",
    )


def local_state(resource_id: str, *, version_local: int | None, parent_id: str | None = None) -> LocalInstallState:
    target_key = f"/{resource_id}/" if parent_id is None else f"/{parent_id}/{resource_id}/"
    return LocalInstallState(
        target_key=target_key,
        resource_id=resource_id,
        parent_id=parent_id,
        parent_target_key=None if parent_id is None else f"/{parent_id}/",
        root_resource_id=resource_id if parent_id is None else parent_id,
        target_kind="root" if parent_id is None else "dependency",
        title=f"Local {resource_id}",
        category_id=8,
        version_local=version_local,
        version_remote=version_local,
        resolved_path=f"C:/downloads/{resource_id}",
        subfolder=None,
        flatten=False,
        protected_files=[],
        is_special=parent_id is None,
        is_watching=False,
        is_licensed=False,
        is_explicit=False,
        is_dependency=parent_id is not None,
    )


def test_plan_allows_downloadable_special_resource():
    target = root_target("151", title="Downloadable Special")
    execution_plan = build_execution_plan(
        desired_set=desired_set_with_targets(target),
        remote_snapshot=RemoteSnapshot(resources={"151": downloadable_remote("151", version_id=2001, title="Downloadable Special")}),
        local_snapshot=LocalSnapshot(),

        settings_env="LIVE",
    )

    action = execution_plan.actions["/151/"]
    assert action.action == "download"
    assert action.reason == "not_installed"


def test_plan_blocks_special_resource_when_user_lacks_access():
    target = root_target("151", title="Blocked Special")
    execution_plan = build_execution_plan(
        desired_set=desired_set_with_targets(target),
        remote_snapshot=RemoteSnapshot(resources={"151": blocked_remote("151", "access_denied")}),
        local_snapshot=LocalSnapshot(),

        settings_env="LIVE",
    )

    action = execution_plan.actions["/151/"]
    assert action.action == "block"
    assert action.reason == "access_denied"


def test_sync_aborts_before_execution_when_discovery_fails(tmp_path):
    db_path = str(tmp_path / "queue_rules.db")
    with patch(
        "redfetch.sync.config.settings",
        SimpleNamespace(ENV="LIVE"),
    ), patch(
        "redfetch.sync.store.load_local_snapshot",
        new=AsyncMock(return_value=LocalSnapshot()),
    ), patch(
        "redfetch.sync.sync_discovery.discover_desired_set",
        new=AsyncMock(side_effect=RuntimeError("metadata exploded")),
    ), patch(
        "redfetch.sync.sync_executor.execute_plan",
        new=AsyncMock(),
    ) as execute_mock:
        with pytest.raises(RuntimeError, match="metadata exploded"):
            asyncio.run(sync.sync(db_path, headers={}, resource_ids=None))

    assert execute_mock.await_count == 0


def test_plan_blocks_stale_special_row_without_fresh_metadata():
    target = root_target("151", title="Stale Special")
    local_snapshot = LocalSnapshot(
        install_targets={
            "/151/": local_state("151", version_local=77),
        }
    )
    execution_plan = build_execution_plan(
        desired_set=desired_set_with_targets(target),
        remote_snapshot=RemoteSnapshot(resources={"151": blocked_remote("151", "no_files")}),
        local_snapshot=local_snapshot,

        settings_env="LIVE",
    )

    action = execution_plan.actions["/151/"]
    assert action.action == "block"
    assert action.reason == "no_files"


def test_plan_blocks_dependency_when_parent_is_blocked():
    root = root_target("151", title="Blocked Parent")
    child = dependency_target("1865", "151")
    execution_plan = build_execution_plan(
        desired_set=desired_set_with_targets(root, child),
        remote_snapshot=RemoteSnapshot(
            resources={
                "151": blocked_remote("151", "access_denied"),
                "1865": downloadable_remote("1865", version_id=1234, title="Dependency Only"),
            }
        ),
        local_snapshot=LocalSnapshot(
            install_targets={
                "/151/": local_state("151", version_local=50),
            }
        ),

        settings_env="LIVE",
    )

    parent_action = execution_plan.actions["/151/"]
    child_action = execution_plan.actions["/151/1865/"]
    assert parent_action.action == "block"
    assert parent_action.reason == "access_denied"
    assert child_action.action == "block"
    assert child_action.reason == "parent_blocked"


def test_targeted_sync_returns_false_when_dependency_in_requested_closure_is_blocked(tmp_path):
    db_path = str(tmp_path / "queue_rules.db")
    root = root_target("151", title="Requested Root")
    root.sources.add("explicit")
    root.explicit_root = True
    child = dependency_target("1865", "151")

    desired_set = desired_set_with_targets(root, child)
    desired_set.mode = "targeted"
    desired_set.requested_root_ids = {"151"}

    remote_snapshot = RemoteSnapshot(
        resources={
            "151": downloadable_remote("151", version_id=2001, title="Requested Root"),
            "1865": blocked_remote("1865", "access_denied"),
        }
    )
    execution_result = ExecutionResult(
        items={
            "/151/": ExecutionResultItem(
                target_key="/151/",
                resource_id="151",
                outcome="downloaded",
                reason="not_installed",
                written_version=2001,
            ),
            "/151/1865/": ExecutionResultItem(
                target_key="/151/1865/",
                resource_id="1865",
                outcome="blocked",
                reason="access_denied",
                written_version=None,
            ),
        }
    )

    with patch(
        "redfetch.sync.config.settings",
        SimpleNamespace(ENV="LIVE"),
    ), patch(
        "redfetch.sync.store.load_local_snapshot",
        new=AsyncMock(return_value=LocalSnapshot()),
    ), patch(
        "redfetch.sync.sync_discovery.discover_desired_set",
        new=AsyncMock(return_value=desired_set),
    ), patch(
        "redfetch.sync.sync_remote.fetch_remote_snapshot",
        new=AsyncMock(return_value=remote_snapshot),
    ), patch(
        "redfetch.sync.sync_executor.execute_plan",
        new=AsyncMock(return_value=execution_result),
    ), patch(
        "redfetch.sync.store.record_installed_state",
        new=AsyncMock(),
    ):
        ok = asyncio.run(sync.sync(db_path, headers={}, resource_ids=["151"]))

    assert ok is False


def test_targeted_sync_returns_true_when_requested_closure_only_untracks_stale_dependency(tmp_path):
    db_path = str(tmp_path / "queue_rules.db")
    root = root_target("151", title="Requested Root")
    root.sources.add("explicit")
    root.explicit_root = True

    desired_set = desired_set_with_targets(root)
    desired_set.mode = "targeted"
    desired_set.requested_root_ids = {"151"}

    local_snapshot = LocalSnapshot(
        install_targets={
            "/151/": local_state("151", version_local=50),
            "/151/1865/": local_state("1865", version_local=50, parent_id="151"),
        }
    )
    remote_snapshot = RemoteSnapshot(
        resources={
            "151": downloadable_remote("151", version_id=2001, title="Requested Root"),
        }
    )
    execution_result = ExecutionResult(
        items={
            "/151/": ExecutionResultItem(
                target_key="/151/",
                resource_id="151",
                outcome="downloaded",
                reason="outdated",
                written_version=2001,
            ),
            "/151/1865/": ExecutionResultItem(
                target_key="/151/1865/",
                resource_id="1865",
                outcome="untracked",
                reason="not_desired",
                written_version=None,
            ),
        }
    )

    with patch(
        "redfetch.sync.config.settings",
        SimpleNamespace(ENV="LIVE"),
    ), patch(
        "redfetch.sync.store.load_local_snapshot",
        new=AsyncMock(return_value=local_snapshot),
    ), patch(
        "redfetch.sync.sync_discovery.discover_desired_set",
        new=AsyncMock(return_value=desired_set),
    ), patch(
        "redfetch.sync.sync_remote.fetch_remote_snapshot",
        new=AsyncMock(return_value=remote_snapshot),
    ), patch(
        "redfetch.sync.sync_executor.execute_plan",
        new=AsyncMock(return_value=execution_result),
    ), patch(
        "redfetch.sync.store.record_installed_state",
        new=AsyncMock(),
    ):
        ok = asyncio.run(sync.sync(db_path, headers={}, resource_ids=["151"]))

    assert ok is True
