"""Cross-process sync lock: a second redfetch process must skip cleanly, not collide."""

import asyncio

from filelock import FileLock

from redfetch import sync
from redfetch.sync_types import SyncOutcome


def _stub_sync(monkeypatch, ran):
    async def _sync(*a, **k):
        ran.append(1)
        return SyncOutcome(success=True)

    monkeypatch.setattr(sync, "sync", _sync)


def test_run_sync_skips_when_another_process_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "_file_lock", None)  # rebuild the singleton from the env var
    ran = []
    _stub_sync(monkeypatch, ran)

    other = FileLock(str(tmp_path / "sync.lock"))  # stands in for another redfetch process
    other.acquire()
    try:
        outcome = asyncio.run(sync.run_sync("db", {}, resource_ids=["1"]))
    finally:
        other.release()

    assert outcome.success is False
    assert ran == []  # never reached the pipeline


def test_run_sync_releases_lock_for_the_next_run(tmp_path, monkeypatch):
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "_file_lock", None)
    ran = []
    _stub_sync(monkeypatch, ran)

    assert asyncio.run(sync.run_sync("db", {}, resource_ids=["1"])).success is True
    assert ran == [1]

    # Lock released -> an outside process could take it now.
    probe = FileLock(str(tmp_path / "sync.lock"))
    probe.acquire(blocking=False)
    probe.release()


def test_busy_message_does_not_name_a_process(tmp_path, monkeypatch, capsys):
    """FileLock alone decides; no sidecar is written and no PID is guessed at."""
    monkeypatch.setenv("REDFETCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "_file_lock", None)
    ran = []
    _stub_sync(monkeypatch, ran)

    other = FileLock(str(tmp_path / "sync.lock"))
    other.acquire()
    try:
        outcome = asyncio.run(sync.run_sync("db", {}, resource_ids=["1"]))
    finally:
        other.release()

    assert outcome.status == "busy"
    out = capsys.readouterr().out
    assert "Another redfetch process is already updating" in out
    assert "PID" not in out
