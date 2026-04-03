"""Regression tests for plan mismatches in the staged sync pipeline."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from redfetch import store
from redfetch import sync_planner as planner
from redfetch.sync_types import (
    DesiredInstallTarget,
    DesiredSet,
    LocalSnapshot,
    PlannedAction,
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


