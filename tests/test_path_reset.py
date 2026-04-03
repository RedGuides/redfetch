"""Tests that the planner detects install-context changes via direct field comparison."""

from redfetch.sync_planner import build_execution_plan
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    LocalInstallState,
    LocalSnapshot,
    RemoteArtifact,
    RemoteResourceState,
    RemoteSnapshot,
)


def _downloadable(resource_id: str) -> RemoteResourceState:
    return RemoteResourceState(
        resource_id=resource_id,
        title=f"Resource {resource_id}",
        category_id=8,
        version_id=100,
        status="downloadable",
        artifact=RemoteArtifact(
            file_id=999,
            filename=f"{resource_id}.zip",
            download_url=f"https://example.com/{resource_id}.zip",
            file_hash=None,
        ),
    )


def _root_target(resource_id: str, *, resolved_path: str, **overrides) -> DesiredInstallTarget:
    fields = dict(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        parent_id=None,
        parent_target_key=None,
        root_resource_id=resource_id,
        target_kind="root",
        sources={"special"},
        title=f"Resource {resource_id}",
        category_id=8,
        resolved_path=resolved_path,
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=False,
    )
    fields.update(overrides)
    return DesiredInstallTarget(**fields)


def _local(resource_id: str, *, resolved_path: str, version_local: int = 100, **overrides) -> LocalInstallState:
    fields = dict(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        parent_id=None,
        parent_target_key=None,
        root_resource_id=resource_id,
        target_kind="root",
        title=f"Resource {resource_id}",
        category_id=8,
        version_local=version_local,
        version_remote=version_local,
        resolved_path=resolved_path,
        subfolder=None,
        flatten=False,
        protected_files=[],
        is_special=True,
        is_watching=False,
        is_licensed=False,
        is_explicit=False,
        is_dependency=False,
    )
    fields.update(overrides)
    return LocalInstallState(**fields)


def test_planner_redownloads_when_resolved_path_changes():
    target = _root_target("153", resolved_path="D:/new_path")
    local = _local("153", resolved_path="C:/old_path")

    plan = build_execution_plan(
        desired_set=DesiredSet(
            mode="full",
            resource_ids={"153"},
            install_targets={target.target_key: target},
        ),
        remote_snapshot=RemoteSnapshot(resources={"153": _downloadable("153")}),
        local_snapshot=LocalSnapshot(install_targets={local.target_key: local}),
        settings_env="LIVE",
    )

    action = plan.actions["/153/"]
    assert action.action == "download"
    assert action.reason == "install_context_changed"


def test_planner_skips_when_path_unchanged_and_version_current():
    target = _root_target("153", resolved_path="C:/same_path")
    local = _local("153", resolved_path="C:/same_path")

    plan = build_execution_plan(
        desired_set=DesiredSet(
            mode="full",
            resource_ids={"153"},
            install_targets={target.target_key: target},
        ),
        remote_snapshot=RemoteSnapshot(resources={"153": _downloadable("153")}),
        local_snapshot=LocalSnapshot(install_targets={local.target_key: local}),
        settings_env="LIVE",
    )

    action = plan.actions["/153/"]
    assert action.action == "skip"
    assert action.reason == "already_current"


def test_planner_redownloads_when_protected_files_change():
    target = _root_target("153", resolved_path="C:/path", protected_files=["a.ini", "b.ini"])
    local = _local("153", resolved_path="C:/path", protected_files=["a.ini"])

    plan = build_execution_plan(
        desired_set=DesiredSet(
            mode="full",
            resource_ids={"153"},
            install_targets={target.target_key: target},
        ),
        remote_snapshot=RemoteSnapshot(resources={"153": _downloadable("153")}),
        local_snapshot=LocalSnapshot(install_targets={local.target_key: local}),
        settings_env="LIVE",
    )

    action = plan.actions["/153/"]
    assert action.action == "download"
    assert action.reason == "install_context_changed"


def test_planner_redownloads_when_flatten_changes():
    target = _root_target("153", resolved_path="C:/path", flatten=True)
    local = _local("153", resolved_path="C:/path", flatten=False)

    plan = build_execution_plan(
        desired_set=DesiredSet(
            mode="full",
            resource_ids={"153"},
            install_targets={target.target_key: target},
        ),
        remote_snapshot=RemoteSnapshot(resources={"153": _downloadable("153")}),
        local_snapshot=LocalSnapshot(install_targets={local.target_key: local}),
        settings_env="LIVE",
    )

    action = plan.actions["/153/"]
    assert action.action == "download"
    assert action.reason == "install_context_changed"


def test_path_change_only_affects_changed_target():
    target_a = _root_target("153", resolved_path="D:/new_path")
    target_b = _root_target("1865", resolved_path="C:/unchanged")
    local_a = _local("153", resolved_path="C:/old_path")
    local_b = _local("1865", resolved_path="C:/unchanged")

    plan = build_execution_plan(
        desired_set=DesiredSet(
            mode="full",
            resource_ids={"153", "1865"},
            install_targets={
                target_a.target_key: target_a,
                target_b.target_key: target_b,
            },
        ),
        remote_snapshot=RemoteSnapshot(resources={
            "153": _downloadable("153"),
            "1865": _downloadable("1865"),
        }),
        local_snapshot=LocalSnapshot(install_targets={
            local_a.target_key: local_a,
            local_b.target_key: local_b,
        }),
        settings_env="LIVE",
    )

    assert plan.actions["/153/"].action == "download"
    assert plan.actions["/153/"].reason == "install_context_changed"
    assert plan.actions["/1865/"].action == "skip"
    assert plan.actions["/1865/"].reason == "already_current"
