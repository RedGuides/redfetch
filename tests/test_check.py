"""Tests the non-interactive update check logic."""
import pytest

from redfetch.config_firstrun import is_configured
from redfetch.sync_types import LocalInstallState, LocalSnapshot
from redfetch.update_check import _count_outdated


@pytest.fixture
def make_state():
    def _make(
        resource_id,
        *,
        version_local=10,
        target_kind="root",
        parent_id=None,
        root_resource_id=None,
    ):
        if target_kind == "root":
            target_key = f"/{resource_id}/"
        else:
            target_key = f"/{parent_id}/{resource_id}/"
        return LocalInstallState(
            target_key=target_key,
            resource_id=resource_id,
            parent_id=parent_id,
            parent_target_key=f"/{parent_id}/" if parent_id else None,
            root_resource_id=root_resource_id or resource_id,
            target_kind=target_kind,
            version_local=version_local,
        )
    return _make


def test_counts_outdated_and_skips_none_version(make_state):
    states = [
        make_state("100", version_local=10),
        make_state("200", version_local=20),
        make_state("300", version_local=None),
    ]
    snapshot = LocalSnapshot(install_targets={s.target_key: s for s in states})
    manifest = {"resources": {"100": {"version_id": 11}, "200": {"version_id": 20}, "300": {"version_id": 99}}}
    result = _count_outdated(snapshot, manifest)
    assert result.updates_available == 1


def test_caller_as_dependency_not_tracked(make_state):
    dep = make_state("1974", version_local=5, target_kind="dependency",
                     parent_id="100", root_resource_id="100")
    snapshot = LocalSnapshot(install_targets={dep.target_key: dep})
    manifest = {"resources": {"1974": {"version_id": 10}}}
    result = _count_outdated(snapshot, manifest, caller_resource_id="1974")
    assert result.updates_available == 1
    assert result.caller_update_available is None


def test_is_configured_false_when_flag_but_no_env(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (tmp_path / "first_run_complete").write_text(str(config_dir))
    assert is_configured(str(tmp_path)) is False
