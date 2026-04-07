"""Regression tests for plan mismatches in the staged sync pipeline."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from redfetch import store
from redfetch import sync_planner as planner
from redfetch.sync_discovery import _add_root_target
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    LocalInstallState,
    LocalSnapshot,
    PLAN_REASON_META,
    PlannedAction,
    PlanReason,
    RemoteArtifact,
    RemoteResourceState,
    RemoteSnapshot,
)


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


def _root_target(resource_id: str, *, explicit: bool = False) -> DesiredInstallTarget:
    sources = {"explicit"} if explicit else {"special"}
    return DesiredInstallTarget(
        target_key=f"/{resource_id}/",
        resource_id=resource_id,
        parent_id=None,
        parent_target_key=None,
        root_resource_id=resource_id,
        target_kind="root",
        sources=sources,
        title=f"Resource {resource_id}",
        category_id=8,
        resolved_path=f"C:/downloads/{resource_id}",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=explicit,
    )


def _dependency_target(resource_id: str, parent_target: DesiredInstallTarget) -> DesiredInstallTarget:
    return DesiredInstallTarget(
        target_key=f"{parent_target.target_key}{resource_id}/",
        resource_id=resource_id,
        parent_id=parent_target.resource_id,
        parent_target_key=parent_target.target_key,
        root_resource_id=parent_target.root_resource_id,
        target_kind="dependency",
        sources={"dependency"},
        title=f"Dependency {resource_id}",
        category_id=8,
        resolved_path=f"C:/downloads/{parent_target.root_resource_id}/{resource_id}",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=False,
    )


def _desired_set(*targets: DesiredInstallTarget, mode: str = "full") -> DesiredSet:
    return DesiredSet(
        mode=mode,
        requested_root_ids={target.resource_id for target in targets if target.explicit_root},
        resource_ids={target.resource_id for target in targets},
        install_targets={target.target_key: target for target in targets},
    )


def _db_path(tmp_path: Path) -> str:
    return str(tmp_path / "sync_pipeline_regression.db")


def test_initialize_schema_preserves_distinct_nested_install_targets(tmp_path):
    db_path = _db_path(tmp_path)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        store.initialize_schema(cursor)
        cursor.execute(
            """
            INSERT INTO downloads (
                target_key, resource_id, parent_id, parent_target_key, root_resource_id, target_kind
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("/151/1865/", 1865, 151, "/151/", 151, "dependency"),
        )
        cursor.execute(
            """
            INSERT INTO downloads (
                target_key, resource_id, parent_id, parent_target_key, root_resource_id, target_kind
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("/303/151/1865/", 1865, 151, "/303/151/", 303, "dependency"),
        )
        conn.commit()

        store.initialize_schema(cursor)

        cursor.execute("SELECT target_key FROM downloads ORDER BY target_key")
        keys = [row[0] for row in cursor.fetchall()]

    assert keys == ["/151/1865/", "/303/151/1865/"]



def test_record_download_success_persists_planner_resolved_path(tmp_path):
    db_path = _db_path(tmp_path)
    with sqlite3.connect(db_path) as conn:
        store.initialize_schema(conn.cursor())
        conn.commit()

    desired_target = DesiredInstallTarget(
        target_key="/5000/",
        resource_id="5000",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="5000",
        target_kind="root",
        sources={"explicit"},
        title="Targeted Root",
        category_id=None,
        resolved_path=None,
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=True,
    )
    remote_state = _downloadable_state("5000", category_id=8)
    action = PlannedAction(
        target_key="/5000/",
        resource_id="5000",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="5000",
        target_kind="root",
        action="download",
        reason="not_installed",
        title="Targeted Root",
        category_id=8,
        remote_version=1234,
        artifact=remote_state.artifact,
        resolved_path="C:/downloads/macros",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=True,
    )

    asyncio.run(
        store.record_download_success(
            db_path,
            target=desired_target,
            action=action,
            remote_state=remote_state,
        )
    )

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT resolved_path, version_local FROM downloads WHERE target_key = '/5000/'")
        row = cursor.fetchone()

    assert row == ("C:/downloads/macros", 1234)


def test_planner_blocks_all_targets_participating_in_cycle():
    root = _root_target("151")
    child = _dependency_target("1865", root)
    repeated = DesiredInstallTarget(
        target_key="/151/1865/151/",
        resource_id="151",
        parent_id="1865",
        parent_target_key="/151/1865/",
        root_resource_id="151",
        target_kind="dependency",
        sources={"dependency"},
        title="Repeated Root",
        category_id=8,
        resolved_path="C:/downloads/151/1865/151",
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=False,
    )

    execution_plan = planner.build_execution_plan(
        desired_set=_desired_set(root, child, repeated),
        remote_snapshot=RemoteSnapshot(
            resources={
                "151": _downloadable_state("151"),
                "1865": _downloadable_state("1865"),
            }
        ),
        local_snapshot=LocalSnapshot(),
        settings_env="LIVE",
    )

    assert execution_plan.actions["/151/1865/"].action == "block"
    assert execution_plan.actions["/151/1865/"].reason == "dependency_cycle"
    assert execution_plan.actions["/151/1865/151/"].action == "block"
    assert execution_plan.actions["/151/1865/151/"].reason == "dependency_cycle"

def test_root_and_dependency_same_resource_id_but_only_dependency_is_outdated():
    root_a = _root_target("153")
    root_b = _root_target("151")
    child_b = _dependency_target("153", root_b)
    local_snapshot = LocalSnapshot(install_targets={
        "/153/": LocalInstallState(
            target_key="/153/",
            resource_id="153",
            root_resource_id="153",
            target_kind="root",
            version_local=10,
            resolved_path="C:/downloads/153",
        ),
        "/151/153/": LocalInstallState(
            target_key="/151/153/",
            resource_id="153",
            parent_id="151",
            parent_target_key="/151/",
            root_resource_id="151",
            target_kind="dependency",
            version_local=7,
            resolved_path="C:/downloads/151/153",
        ),
    })
    remote_snapshot = RemoteSnapshot(
    resources={
    "153": RemoteResourceState(
        resource_id="153",
        title="Resource 153",
        category_id=8,
        version_id=10,
        status="downloadable",
        artifact=RemoteArtifact(
            file_id=9876,
            filename="153.zip",
            download_url="https://example.com/153.zip",
            file_hash="d41d8cd98f00b204e9800998ecf8427e",
        ),
        source_note="manifest_plus_access_check",
    ),
    "151": RemoteResourceState(
        resource_id="151",
        title="Resource 151",
        category_id=8,
        version_id=10,
        status="downloadable",
        artifact=RemoteArtifact(
            file_id=9876,
            filename="151.zip",
            download_url="https://example.com/151.zip",
            file_hash="d41d8cd98f00b204e9800998ecf8427e",
        ),
        source_note="manifest_plus_access_check",
    ),
    })
    execution_plan = planner.build_execution_plan(
        desired_set=_desired_set(root_a, root_b, child_b),
        remote_snapshot=remote_snapshot,
        local_snapshot=local_snapshot,
        settings_env="LIVE",
    )
    assert execution_plan.actions["/153/"].action == "skip"
    assert execution_plan.actions["/153/"].reason == "already_current"
    assert execution_plan.actions["/151/153/"].action == "download"
    assert execution_plan.actions["/151/153/"].reason == "outdated"

def test_reset_download_date_for_resource_does_not_reset_unrelated_dependency_occurrences(tmp_path):
    db_path = _db_path(tmp_path)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        store.initialize_schema(cursor)
        cursor.execute(
            """
            INSERT INTO downloads (
                target_key, resource_id, parent_id, parent_target_key, root_resource_id,
                target_kind, version_local
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("/153/", 153, 0, None, 153, "root", 9),
        )
        cursor.execute(
            """
            INSERT INTO downloads (
                target_key, resource_id, parent_id, parent_target_key, root_resource_id,
                target_kind, version_local
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("/151/153/", 153, 151, "/151/", 151, "dependency", 7),
        )
        conn.commit()

        store.reset_versions_for_resource(cursor, "153")
        conn.commit()

        cursor.execute("SELECT target_key, version_local FROM downloads ORDER BY target_key")
        rows = cursor.fetchall()

    assert rows == [
        ("/151/153/", 7),
        ("/153/", 0),
    ]


def test_staff_pick_without_default_path_gets_category_subfolder():
    """Staff picks (opt_in=True, no default_path) should be placed in the
    category subfolder under VanillaMQ (e.g. VanillaMQ_LIVE/lua), not dumped
    at the VanillaMQ root.

    Regression: discovery was eagerly calling resolve_root_path with
    category_id=None for these resources, baking in a path that lacked the
    category subfolder.  The planner then short-circuited because
    resolved_path was already set."""
    download_folder = "C:/test/Downloads"
    special_resources = {
        "1974": {"default_path": "VanillaMQ_LIVE", "custom_path": "", "opt_in": True},
        "2539": {"opt_in": True, "staff_pick": True},
    }
    mock_settings = MagicMock()
    mock_settings.ENV = "LIVE"
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER=download_folder,
        SPECIAL_RESOURCES=special_resources,
        PROTECTED_FILES_BY_RESOURCE={},
    )

    with patch("redfetch.config.settings", mock_settings), \
         patch("redfetch.config.VANILLA_MAP", {1974: "LIVE"}), \
         patch("redfetch.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}):

        # Discovery: staff pick added from config only (no API payload)
        desired_set = DesiredSet(mode="full")
        target = _add_root_target(
            desired_set,
            resource_id="2539",
            sources={"special"},
            payload=None,
            settings_env="LIVE",
        )

        assert target.resolved_path is None, (
            f"Discovery should defer path resolution for staff picks without "
            f"default_path, got {target.resolved_path}"
        )

        # Planner resolves using remote state's category_id
        remote_snapshot = RemoteSnapshot(
            resources={
                "2539": _downloadable_state("2539", category_id=25),
            }
        )

        plan = planner.build_execution_plan(
            desired_set=desired_set,
            remote_snapshot=remote_snapshot,
            local_snapshot=LocalSnapshot(),
            settings_env="LIVE",
        )

    action = plan.actions["/2539/"]
    assert action.action == "download"
    expected = os.path.normpath(os.path.join(download_folder, "VanillaMQ_LIVE", "lua"))
    assert action.resolved_path == expected, (
        f"Expected {expected}, got {action.resolved_path}. "
        "Staff pick should land in VanillaMQ_LIVE/lua, not at the VanillaMQ root."
    )


def test_targeted_sync_blocks_resource_with_unknown_category():
    """Resources outside CATEGORY_MAP (non-MQ categories like guides/configs)
    should be blocked, not silently downloaded to the base folder.

    Regression: the refactored targeted-sync path in _root_sources_for_targeted
    stopped checking categories, so any explicitly-requested resource would
    download regardless of category."""
    download_folder = "C:/test/Downloads"
    mock_settings = MagicMock()
    mock_settings.ENV = "LIVE"
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER=download_folder,
        SPECIAL_RESOURCES={},
        PROTECTED_FILES_BY_RESOURCE={},
    )

    # category_id=99 is not in CATEGORY_MAP
    target = DesiredInstallTarget(
        target_key="/3112/",
        resource_id="3112",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="3112",
        target_kind="root",
        sources={"explicit"},
        title=None,
        category_id=None,
        resolved_path=None,
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=True,
    )
    desired_set = DesiredSet(
        mode="targeted",
        requested_root_ids={"3112"},
        resource_ids={"3112"},
        install_targets={"/3112/": target},
    )

    with patch("redfetch.config.settings", mock_settings), \
         patch("redfetch.sync_planner.config.settings", mock_settings), \
         patch("redfetch.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}), \
         patch("redfetch.sync_planner.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}):
        plan = planner.build_execution_plan(
            desired_set=desired_set,
            remote_snapshot=RemoteSnapshot(
                resources={
                    "3112": _downloadable_state("3112", category_id=99),
                }
            ),
            local_snapshot=LocalSnapshot(),
            settings_env="LIVE",
        )

    action = plan.actions["/3112/"]
    assert action.action == "block", (
        f"Expected block for non-MQ category, got {action.action}"
    )
    assert action.reason == "unknown_category"


def test_special_resource_with_unknown_category_still_downloads():
    """Resources in SPECIAL_RESOURCES should bypass the category check,
    even if their category isn't in CATEGORY_MAP."""
    download_folder = "C:/test/Downloads"
    special_resources = {
        "3112": {"default_path": "MySpecialApp", "custom_path": "", "opt_in": True},
    }
    mock_settings = MagicMock()
    mock_settings.ENV = "LIVE"
    mock_settings.from_env.return_value = SimpleNamespace(
        DOWNLOAD_FOLDER=download_folder,
        SPECIAL_RESOURCES=special_resources,
        PROTECTED_FILES_BY_RESOURCE={},
    )

    target = DesiredInstallTarget(
        target_key="/3112/",
        resource_id="3112",
        parent_id=None,
        parent_target_key=None,
        root_resource_id="3112",
        target_kind="root",
        sources={"explicit", "special"},
        title=None,
        category_id=None,
        resolved_path=None,
        subfolder=None,
        flatten=False,
        protected_files=[],
        explicit_root=True,
    )
    desired_set = DesiredSet(
        mode="targeted",
        requested_root_ids={"3112"},
        resource_ids={"3112"},
        install_targets={"/3112/": target},
    )

    with patch("redfetch.config.settings", mock_settings), \
         patch("redfetch.sync_planner.config.settings", mock_settings), \
         patch("redfetch.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}), \
         patch("redfetch.sync_planner.config.CATEGORY_MAP", {8: "macros", 11: "plugins", 25: "lua"}):
        plan = planner.build_execution_plan(
            desired_set=desired_set,
            remote_snapshot=RemoteSnapshot(
                resources={
                    "3112": _downloadable_state("3112", category_id=99),
                }
            ),
            local_snapshot=LocalSnapshot(),
            settings_env="LIVE",
        )

    action = plan.actions["/3112/"]
    assert action.action == "download", (
        f"Special resource should bypass category check, got {action.action} ({action.reason})"
    )
    expected = os.path.normpath(os.path.join(download_folder, "MySpecialApp"))
    assert action.resolved_path == expected


def test_plan_reason_meta_covers_all_reasons():
    """Every PlanReason value must have a corresponding PLAN_REASON_META entry."""
    from typing import get_args

    all_reasons = set(get_args(PlanReason))
    covered = set(PLAN_REASON_META)
    missing = all_reasons - covered
    extra = covered - all_reasons
    assert not missing, f"PlanReason values missing from PLAN_REASON_META: {missing}"
    assert not extra, f"PLAN_REASON_META has keys not in PlanReason: {extra}"


