"""Tests for the shared shortcuts registry (redfetch run / open)."""

import pytest

from redfetch import shortcuts, processes


# --- registry integrity -----------------------------------------------------

def test_no_duplicate_names_within_each_namespace():
    run_names = [n for r in shortcuts.RUNNABLES for n in (r.key, *r.aliases)]
    open_names = [n for o in shortcuts.OPENABLES for n in (o.key, *o.aliases)]
    assert len(run_names) == len(set(run_names)), run_names
    assert len(open_names) == len(set(open_names)), open_names


def test_every_run_name_resolves_case_insensitively():
    for r in shortcuts.RUNNABLES:
        for name in (r.key, *r.aliases):
            assert shortcuts.find_runnable(name) is r
            assert shortcuts.find_runnable(name.upper()) is r
            assert shortcuts.find_runnable(f"  {name} ") is r


def test_every_open_name_resolves_case_insensitively():
    for o in shortcuts.OPENABLES:
        for name in (o.key, *o.aliases):
            assert shortcuts.find_openable(name) is o
            assert shortcuts.find_openable(name.upper()) is o


def test_unknown_name_returns_none():
    assert shortcuts.find_runnable("bogus") is None
    assert shortcuts.find_openable("bogus") is None


# --- registry contents (guards against accidental edits) --------------------

def test_known_static_attributes():
    assert shortcuts.find_runnable("mq").executable == "MacroQuest.exe"
    assert shortcuts.find_runnable("eqgame").args == ("patchme",)
    assert shortcuts.find_runnable("meshupdater").executable == "MeshUpdater.exe"
    assert shortcuts.find_openable("settings").filename == "settings.local.toml"
    assert shortcuts.find_openable("mq-config").css == "file"
    assert shortcuts.find_openable("downloads").filename is None  # a folder


# --- run() dispatch ---------------------------------------------------------

def test_run_passes_resolved_dir_and_merged_args(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args: calls.append((folder, exe, args)))
    r = shortcuts.Runnable("t", "L", "Foo.exe", lambda: "C:/x", args=("a",))
    shortcuts.run(r, extra=["b"])
    assert calls == [("C:/x", "Foo.exe", ["a", "b"])]


def test_run_without_extra_args(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, "run_executable",
                        lambda folder, exe, args: calls.append((folder, exe, args)))
    r = shortcuts.Runnable("t", "L", "Foo.exe", lambda: "C:/x")
    shortcuts.run(r)
    assert calls == [("C:/x", "Foo.exe", [])]


# --- open_target() dispatch -------------------------------------------------

def test_open_folder_dispatch(monkeypatch):
    opened = []
    monkeypatch.setattr(processes, "open_folder", lambda path: opened.append(path))
    o = shortcuts.Openable("t", "L", lambda: "C:/d")
    assert shortcuts.open_target(o) == ""
    assert opened == ["C:/d"]


def test_open_file_dispatch_returns_descriptor(monkeypatch):
    opened = []
    monkeypatch.setattr(processes, "open_file",
                        lambda folder, name: opened.append((folder, name)) or "with Notepad")
    o = shortcuts.Openable("t", "L", lambda: "C:/d", "f.ini")
    assert shortcuts.open_target(o) == "with Notepad"
    assert opened == [("C:/d", "f.ini")]


def test_open_runs_prepare_hook_before_opening(monkeypatch):
    events = []
    monkeypatch.setattr(processes, "open_file",
                        lambda folder, name: events.append("open") or "")
    o = shortcuts.Openable("t", "L", lambda: "C:/d", "f.ini",
                           prepare=lambda: events.append("prepare"))
    shortcuts.open_target(o)
    assert events == ["prepare", "open"]


def test_open_missing_path_raises(monkeypatch):
    o = shortcuts.Openable("t", "L", lambda: None)
    with pytest.raises(FileNotFoundError):
        shortcuts.open_target(o)


# --- availability -----------------------------------------------------------

def test_openable_available_folder(tmp_path):
    o = shortcuts.Openable("t", "L", lambda: str(tmp_path))
    assert shortcuts.openable_available(o) is True
    missing = shortcuts.Openable("t", "L", lambda: str(tmp_path / "nope"))
    assert shortcuts.openable_available(missing) is False
    unset = shortcuts.Openable("t", "L", lambda: None)
    assert shortcuts.openable_available(unset) is False


def test_openable_available_file(tmp_path):
    (tmp_path / "f.ini").write_text("")
    present = shortcuts.Openable("t", "L", lambda: str(tmp_path), "f.ini")
    assert shortcuts.openable_available(present) is True
    absent = shortcuts.Openable("t", "L", lambda: str(tmp_path), "missing.ini")
    assert shortcuts.openable_available(absent) is False
