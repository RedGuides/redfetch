"""fetch_remote_snapshot builds RemoteResourceStates from the manifest + rgsync, no network."""

from redfetch.sync_remote import fetch_remote_snapshot
from redfetch.sync_types import DesiredSet, SyncInfo

HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _entry(*, version_id=100, title="R", parent_category_id=8,
           access_tier="public", requires_license=False, files=1):
    current_files = [
        {"id": 1000 + i, "filename": f"f{i}.zip", "download_url": f"https://x/{i}", "hash": HASH}
        for i in range(files)
    ]
    return {
        "version_id": version_id,
        "title": title,
        "parent_category_id": parent_category_id,
        "access_tier": access_tier,
        "requires_license": requires_license,
        "current_files": current_files,
    }


def _snapshot(manifest_entries, sync_info):
    manifest = {"resources": manifest_entries}
    desired = DesiredSet(mode="full", resource_ids=set(manifest_entries) | {"absent"})
    return fetch_remote_snapshot(desired_set=desired, manifest=manifest, sync_info=sync_info)


def _member(**kw):
    kw.setdefault("is_level_2", False)
    kw.setdefault("is_moderator", False)
    return SyncInfo(**kw)


def test_status_resolution_for_a_plain_member():
    entries = {
        "public1":   _entry(access_tier="public", files=1),
        "level2":    _entry(access_tier="level2", files=1),
        "restricted": _entry(access_tier="restricted", files=1),
        "licreq":    _entry(access_tier="public", requires_license=True, files=1),
        "nofiles":   _entry(access_tier="public", files=0),
        "multi":     _entry(access_tier="public", files=2),
    }
    snap = _snapshot(entries, _member())

    assert snap.resources["public1"].status == "downloadable"
    assert snap.resources["level2"].status == "needs_level_2"
    assert snap.resources["restricted"].status == "access_denied"
    assert snap.resources["licreq"].status == "needs_license"
    assert snap.resources["nofiles"].status == "no_files"
    assert snap.resources["multi"].status == "multiple_files"

    absent = snap.resources["absent"]
    assert absent.status == "not_found"
    assert absent.artifact is None and absent.version_id is None


def test_manifest_fields_flow_into_the_state():
    entries = {"7": _entry(version_id=555, title="Guidestone", parent_category_id=8, files=1)}
    state = _snapshot(entries, _member()).resources["7"]

    assert state.version_id == 555
    assert state.title == "Guidestone"
    assert state.category_id == 8
    assert state.artifact is not None
    assert state.artifact.file_hash == HASH


def test_artifact_only_present_when_downloadable():
    entries = {"blocked": _entry(access_tier="level2", files=1)}   # member lacks level 2
    state = _snapshot(entries, _member()).resources["blocked"]
    assert state.status == "needs_level_2"
    assert state.artifact is None
    assert state.version_id == 100   # version still carried for the planner


def test_sync_info_flags_unlock_access():
    entries = {"level2": _entry(access_tier="level2"), "licreq": _entry(requires_license=True)}
    snap = _snapshot(entries, _member(is_level_2=True, licensed_ids={"licreq"}))
    assert snap.resources["level2"].status == "downloadable"      # is_level_2 unlocks the tier
    assert snap.resources["licreq"].status == "downloadable"      # held license unlocks the gate


def test_moderator_bypasses_every_access_gate():
    entries = {
        "level2":     _entry(access_tier="level2", files=1),
        "restricted": _entry(access_tier="restricted", files=1),
        "licreq":     _entry(requires_license=True, files=1),
        "nofiles":    _entry(files=0),
    }
    snap = _snapshot(entries, _member(is_moderator=True))
    assert snap.resources["level2"].status == "downloadable"
    assert snap.resources["restricted"].status == "downloadable"
    assert snap.resources["licreq"].status == "downloadable"
    assert snap.resources["nofiles"].status == "no_files"   # access passes, file reality remains
