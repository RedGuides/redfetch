"""The provisioning UI: the worker, its lockdown, and the Add dialog's second mode.

Same posture as test_tui_routing.py -- no App is mounted, so every widget and
every app reactive is a plain stand-in.
"""
import asyncio
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from redfetch import config
from redfetch import tui_servers

# tui's Redfetch class evaluates config.settings.ENV at import time (a
# reactive default); stand in for it if config isn't initialized, then restore.
_prior_settings = config.settings
if _prior_settings is None:
    config.settings = SimpleNamespace(ENV="LIVE")
try:
    from redfetch import tui
finally:
    config.settings = _prior_settings

from conftest import FakeInput, _eq_folder

AddServerScreen = tui_servers.AddServerScreen


# --- the worker and its gates -------------------------------------------------


def _provision_app(monkeypatch, **flags):
    """A Redfetch stand-in for provision_server and the worker body."""
    notices, posted, switched = [], [], []
    fields = dict(
        is_updating=False, interface_running=False, patcher_install_running=False,
        laa_enable_running=False, provision_running=False, provision_cancellable=False,
        current_env="EMU",
        notify=lambda msg, **kw: notices.append((str(msg), kw)),
        post_message=lambda message: posted.append(message),
        refresh_after_server_change=lambda: posted.append("refresh"),
        _provision_cancel=None,
        _provision_worker=lambda payload, **kw: posted.append(("worker", payload, kw)),
    )
    app = SimpleNamespace(**{**fields, **flags})
    # Records the latch as it stood at switch time: a switch while it's still set
    # would be refused, and refused silently.
    app.switch_active_server = lambda slug: switched.append((slug, app.provision_running))
    monkeypatch.setattr(tui, "get_current_worker",
                        lambda: SimpleNamespace(is_cancelled=False))
    return app, SimpleNamespace(notices=notices, posted=posted, switched=switched)


@pytest.mark.parametrize(
    "flag",
    ["is_updating", "interface_running", "patcher_install_running",
     "laa_enable_running", "provision_running"],
)
def test_provision_refuses_while_anything_else_runs(monkeypatch, flag):
    """One long-running operation at a time; a provision owns the app while it runs."""
    app, log = _provision_app(monkeypatch, **{flag: True})
    assert tui.Redfetch.provision_server(app, {"slug": "lazarus"}) is False
    assert log.posted == []


def test_provision_refuses_a_slug_another_terminal_already_configured(monkeypatch):
    """The CLI can claim the slug while the dialog sits open."""
    app, log = _provision_app(monkeypatch)
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: True)
    monkeypatch.setattr(tui.servers, "server_label", lambda slug, env: "Project Lazarus")

    assert tui.Redfetch.provision_server(app, {"slug": "lazarus"}) is False

    assert app.provision_running is False
    assert "already has an EverQuest folder" in log.notices[0][0]


def test_provision_latches_and_dispatches(monkeypatch):
    app, log = _provision_app(monkeypatch)
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: False)
    monkeypatch.setattr(tui.servers, "list_servers", lambda env: {"lazarus": {}})

    assert tui.Redfetch.provision_server(app, {"slug": "lazarus"}, switch_after=True) is True

    assert app.provision_running is True and app.provision_cancellable is True
    assert log.posted[0][2] == {"env": "EMU", "switch_after": True}


class _OrderRecordingApp(SimpleNamespace):
    """Records what provision_cancellable held when the repainting latch flipped."""

    @property
    def provision_running(self):
        return self._running

    @provision_running.setter
    def provision_running(self, value):
        self.cancellable_at_latch = self.provision_cancellable
        self._running = value


def test_the_cancel_flag_is_set_before_the_latch_that_repaints(monkeypatch):
    """provision_running is the reactive tabs watch, so it drives the repaint. A
    Cancel button painted while cancellable is still False never heals."""
    app = _OrderRecordingApp(
        _running=False, is_updating=False, interface_running=False,
        patcher_install_running=False, laa_enable_running=False,
        provision_cancellable=False, cancellable_at_latch=None, current_env="EMU",
        notify=lambda *a, **k: None,
        _provision_cancel=None,
        _provision_worker=lambda payload, **kw: None,
    )
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: False)
    monkeypatch.setattr(tui.servers, "list_servers", lambda env: {"lazarus": {}})

    tui.Redfetch.provision_server(app, {"slug": "lazarus"})

    assert app.cancellable_at_latch is True


def _run_worker(monkeypatch, app, outcome, *, switch_after=False):
    """Drive the worker body against a stand-in provision core."""
    async def fake_provision(slug, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tui.provision, "provision", fake_provision)
    body = tui.Redfetch._provision_worker.__wrapped__
    payload = {"slug": "lazarus", "source": "D:/rof2.zip", "destination": "D:/EQ-Laz"}
    return asyncio.run(body(app, payload, env="EMU", switch_after=switch_after))


def test_worker_clears_the_latch_before_switching(monkeypatch):
    """switch_active_server refuses while provision_running, and refuses silently."""
    app, log = _provision_app(monkeypatch, provision_running=True, provision_cancellable=True)
    monkeypatch.setattr(tui.servers, "server_label", lambda slug, env: "Project Lazarus")
    result = tui.provision.ProvisionResult(Path("D:/EQ-Laz"), ())

    assert _run_worker(monkeypatch, app, result, switch_after=True) is True

    assert app.provision_running is False and app.provision_cancellable is False
    assert log.switched == [("lazarus", False)]


def test_worker_styles_a_cancel_as_a_notice(monkeypatch):
    """ProvisionCancelled subclasses ProvisionError, so it has to be caught first."""
    app, log = _provision_app(monkeypatch, provision_running=True)
    cancelled = tui.provision.ProvisionCancelled("Setup was cancelled, so nothing was created.")

    assert _run_worker(monkeypatch, app, cancelled, switch_after=True) is False

    message, kwargs = log.notices[0]
    assert kwargs["severity"] == "warning"
    assert "cancelled" in message
    assert log.switched == []  # nothing was created to switch to


def test_worker_reports_a_failure_as_an_error(monkeypatch):
    app, log = _provision_app(monkeypatch, provision_running=True)
    failure = tui.provision.ProvisionError("There isn't enough room in D:/downloads.")

    assert _run_worker(monkeypatch, app, failure) is False

    message, kwargs = log.notices[0]
    assert "enough room" in message
    assert kwargs == {"severity": "error", "markup": False}
    assert app.provision_running is False


def test_worker_surfaces_each_notice(monkeypatch):
    app, log = _provision_app(monkeypatch, provision_running=True)
    monkeypatch.setattr(tui.servers, "server_label", lambda slug, env: "Project Lazarus")
    result = tui.provision.ProvisionResult(Path("D:/EQ-Laz"), ("the patcher didn't run",))

    _run_worker(monkeypatch, app, result)

    warnings = [m for m, kw in log.notices if kw.get("severity") == "warning"]
    assert warnings == ["the patcher didn't run"]
    # Free-text server labels and paths throughout, so markup stays off.
    assert all(kw.get("markup") is False for _m, kw in log.notices)


def test_worker_points_at_the_folder_when_registration_fails(monkeypatch):
    """add_server raises ValueError after the copy; the tree is real, just unregistered."""
    app, log = _provision_app(monkeypatch, provision_running=True)

    assert _run_worker(monkeypatch, app, ValueError("that name is taken")) is False

    message, kwargs = log.notices[0]
    assert "D:/EQ-Laz" in message and "use an existing folder" in message
    assert kwargs == {"severity": "error", "markup": False}
    assert app.provision_running is False  # the finally still ran


def test_worker_posts_progress_instead_of_calling_from_the_thread(monkeypatch):
    """The core reports from the copy thread AND this one; only post_message is safe on both."""
    app, log = _provision_app(monkeypatch, provision_running=True)
    monkeypatch.setattr(tui.servers, "server_label", lambda slug, env: "Project Lazarus")

    async def reporting(slug, **kwargs):
        kwargs["progress"]("Reading fixture.zip", None)
        kwargs["progress"]("Copying", 0.5)
        return tui.provision.ProvisionResult(Path("D:/EQ-Laz"), ())

    monkeypatch.setattr(tui.provision, "provision", reporting)
    body = tui.Redfetch._provision_worker.__wrapped__
    asyncio.run(body(app, {"slug": "lazarus", "source": "s", "destination": "d"},
                     env="EMU", switch_after=False))

    progress = [m for m in log.posted if isinstance(m, tui_servers.ProvisionProgress)]
    assert [(m.label, m.fraction) for m in progress] == [
        ("Reading fixture.zip", None), ("Copying", 0.5),
    ]


def test_cancel_declines_once_the_folder_has_landed(monkeypatch):
    app, log = _provision_app(monkeypatch, provision_running=True, provision_cancellable=False)
    app._provision_cancel = threading.Event()

    tui.Redfetch.cancel_provision(app)

    assert app._provision_cancel.is_set() is False
    assert "Too late" in log.notices[0][0]


def test_cancel_sets_the_flag_during_the_copy(monkeypatch):
    app, log = _provision_app(monkeypatch, provision_running=True, provision_cancellable=True)
    app._provision_cancel = threading.Event()

    tui.Redfetch.cancel_provision(app)

    assert app._provision_cancel.is_set() is True


def test_the_move_report_takes_cancel_off_the_table(monkeypatch):
    """The one progress label the app reads, rather than just displays."""
    app, _log = _provision_app(monkeypatch, provision_running=True, provision_cancellable=True)
    app._base_main_screen = lambda: None

    tui.Redfetch.on_provision_progress(
        app, tui_servers.ProvisionProgress(tui.provision.FINISHING_LABEL, None))

    assert app.provision_cancellable is False


def test_ordinary_progress_leaves_cancel_alone(monkeypatch):
    app, _log = _provision_app(monkeypatch, provision_running=True, provision_cancellable=True)
    app._base_main_screen = lambda: None

    tui.Redfetch.on_provision_progress(app, tui_servers.ProvisionProgress("Copying", 0.5))

    assert app.provision_cancellable is True


# --- the lockdown census ------------------------------------------------------


def test_easy_update_refuses_to_start_during_a_provision():
    started = []
    app = SimpleNamespace(
        is_updating=False, provision_running=True,
        notify=lambda *a, **k: None,
        _update_watched_worker=lambda: started.append("watched"),
    )
    tui.Redfetch.handle_update_watched(app)
    assert started == []


def test_single_resource_update_refuses_during_a_provision():
    """Reachable from the command palette, which walks past every disabled widget."""
    started = []
    app = SimpleNamespace(
        is_updating=False, provision_running=True,
        notify=lambda *a, **k: None,
        _get_main_screen=lambda: SimpleNamespace(query_one=lambda sel, _t=None: FakeInput("123")),
        _update_single_resource_worker=lambda rid: started.append(rid),
    )
    tui.Redfetch.handle_update_resource_id(app)
    assert started == []


def test_clearing_the_download_cache_refuses_during_a_provision():
    started = []
    app = SimpleNamespace(
        is_updating=False, provision_running=True,
        notify=lambda *a, **k: None,
        _reset_downloads_worker=lambda: started.append("reset"),
    )
    tui.Redfetch.handle_reset_downloads(app)
    assert started == []


def test_the_rg_interface_refuses_to_start_during_a_provision():
    """interface_running greys the whole Servers tab, Cancel included — so a provision
    has to refuse it, not just survive it."""
    started = []
    app = SimpleNamespace(
        provision_running=True, interface_running=False,
        notify=lambda *a, **k: None,
        _prepare_redguides_interface_worker=lambda: started.append("interface"),
    )
    tui.Redfetch.handle_redguides_interface(app)
    assert started == [] and app.interface_running is False


def test_the_rg_interface_key_is_hidden_during_a_provision():
    """A footer key the handler refuses is a dead press (the P5 mirror-the-gate rule)."""
    app = SimpleNamespace(interface_running=False, provision_running=True)
    assert tui.Redfetch.check_action(app, "start_interface", ()) is False


def test_provision_says_so_when_it_refuses():
    """The tab's _busy() misses the patcher and LAA runs, so a silent return would
    close a filled-in dialog and do nothing."""
    notices = []
    app = SimpleNamespace(
        is_updating=False, interface_running=False, patcher_install_running=True,
        laa_enable_running=False, provision_running=False, current_env="EMU",
        notify=lambda msg, **kw: notices.append((str(msg), kw)),
    )

    assert tui.Redfetch.provision_server(app, {"slug": "lazarus"}) is False

    assert "busy" in notices[0][0]


def test_switching_servers_refuses_during_a_provision(monkeypatch):
    """The chain writes config at its end; a switch mid-run would race it."""
    started = []
    monkeypatch.setattr(tui.servers, "switch_to_generic", lambda env: started.append(env))
    app = SimpleNamespace(
        is_updating=False, interface_running=False, provision_running=True,
        current_env="EMU", notify=lambda *a, **k: None,
    )
    tui.Redfetch.switch_to_bare_setup(app, "EMU")

    assert started == []


# --- AddServerScreen: mode choice, prefill, and payloads ----------------------


class _RecordingPrevent:
    """Stands in for Screen.prevent, recording what each call suppressed."""

    def __init__(self):
        self.suppressed = []

    def __call__(self, *events):
        self.suppressed.append(events)
        return nullcontext()


def _dialog(monkeypatch, *, source="", provisioning=False, custom=False, locked=None,
            folder="", slug="lazarus", touched=False):
    """An AddServerScreen stand-in carrying the widgets its handlers reach for."""
    errors = []
    dismissed = []
    widgets = {
        "#server_dialog_error": SimpleNamespace(
            update=lambda msg: errors.append(getattr(msg, "plain", None) or str(msg))),
        "#add_folder": FakeInput(folder),
        "#add_source": FakeInput(source),
        "#add_slug": FakeInput(slug),
        "#add_label": FakeInput(""),
        "#add_patcher_url": FakeInput(""),
        "#add_patcher_exe": FakeInput(""),
        "#add_known": FakeInput(slug),
    }
    monkeypatch.setattr(tui_servers.provision, "clean_source", lambda: source)
    screen = SimpleNamespace(
        query_one=lambda selector, _type=None: widgets[selector],
        prevent=_RecordingPrevent(),
        _is_custom=lambda: custom,
        _is_provision=lambda: provisioning,
        _current_slug=lambda: slug,
        _locked=locked,
        _destination_touched=touched,
        _remember_source=lambda path: True,
        BROWSE=AddServerScreen.BROWSE,
        PROVISION=AddServerScreen.PROVISION,
        dismiss=lambda payload: dismissed.append(payload),
    )
    return screen, widgets, dismissed, errors


def test_dialog_opens_in_provision_mode_once_a_source_is_remembered(monkeypatch, tmp_path):
    archive = tmp_path / "rof2.zip"
    archive.write_bytes(b"PK")
    screen, *_ = _dialog(monkeypatch, source=str(archive))
    assert AddServerScreen._default_mode(screen) == AddServerScreen.PROVISION


def test_dialog_falls_back_to_browse_when_the_source_is_gone(monkeypatch, tmp_path):
    """A remembered path that no longer exists can't be the default answer."""
    screen, *_ = _dialog(monkeypatch, source=str(tmp_path / "moved.zip"))
    assert AddServerScreen._default_mode(screen) == AddServerScreen.BROWSE


def test_dialog_prefills_the_destination_from_the_chosen_server(monkeypatch):
    screen, widgets, *_ = _dialog(monkeypatch, provisioning=True)
    monkeypatch.setattr(tui_servers.provision, "default_destination",
                        lambda slug: f"D:/downloads/EverQuest_{slug}")

    AddServerScreen._sync_destination(screen)

    assert widgets["#add_folder"].value == "D:/downloads/EverQuest_lazarus"


def test_the_prefill_suppresses_its_own_change_event(monkeypatch):
    """Textual raises Input.Changed for programmatic writes too. Without prevent(),
    the very first prefill marks the field hand-edited and the live update dies."""
    screen, _widgets, *_ = _dialog(monkeypatch, provisioning=True)
    monkeypatch.setattr(tui_servers.provision, "default_destination", lambda slug: "D:/x")

    AddServerScreen._sync_destination(screen)

    assert screen.prevent.suppressed == [(tui_servers.Input.Changed,)]
    assert screen._destination_touched is False


def test_typing_in_the_destination_marks_it_hand_edited(monkeypatch):
    screen, _widgets, *_ = _dialog(monkeypatch, provisioning=True)

    AddServerScreen.handle_folder_changed(screen, event=None)

    assert screen._destination_touched is True


def test_choosing_the_clean_copy_mode_frees_the_destination_again(monkeypatch):
    """A folder typed to answer the other question shouldn't suppress the prefill."""
    screen, widgets, *_ = _dialog(
        monkeypatch, provisioning=True, folder="E:/typed for browse", touched=True)
    monkeypatch.setattr(tui_servers.provision, "default_destination",
                        lambda slug: "D:/downloads/EverQuest_lazarus")
    screen._sync_mode = lambda: None
    screen._sync_destination = lambda: AddServerScreen._sync_destination(screen)

    AddServerScreen.handle_mode_changed(screen, event=None)

    assert screen._destination_touched is False
    assert widgets["#add_folder"].value == "D:/downloads/EverQuest_lazarus"


def test_dialog_leaves_a_hand_edited_destination_alone(monkeypatch):
    screen, widgets, *_ = _dialog(
        monkeypatch, provisioning=True, folder="E:/my own folder", touched=True)

    AddServerScreen._sync_destination(screen)

    assert widgets["#add_folder"].value == "E:/my own folder"


def test_dialog_carries_the_source_and_destination_in_the_payload(monkeypatch, tmp_path):
    """The worker never re-reads the saved source: a typed one may not be saved yet."""
    archive = tmp_path / "rof2.zip"
    archive.write_bytes(b"PK")
    destination = tmp_path / "new"
    screen, _widgets, dismissed, errors = _dialog(
        monkeypatch, provisioning=True, source=str(archive), folder=str(destination))

    AddServerScreen._confirm(screen)

    assert errors == []
    assert dismissed[0]["mode"] == AddServerScreen.PROVISION
    assert dismissed[0]["source"] == str(archive)
    assert dismissed[0]["destination"] == str(destination)


def test_dialog_refuses_a_source_that_isnt_a_rof2_copy(monkeypatch, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("hi")
    screen, _widgets, dismissed, errors = _dialog(
        monkeypatch, provisioning=True, source=str(junk), folder=str(tmp_path / "new"))

    AddServerScreen._confirm(screen)

    assert not dismissed
    assert ".zip" in errors[0]


def test_dialog_refuses_a_destination_that_already_holds_an_install(monkeypatch, tmp_path):
    archive = tmp_path / "rof2.zip"
    archive.write_bytes(b"PK")
    occupied = _eq_folder(tmp_path)
    screen, _widgets, dismissed, errors = _dialog(
        monkeypatch, provisioning=True, source=str(archive), folder=str(occupied))

    AddServerScreen._confirm(screen)

    assert not dismissed
    assert "use an existing folder" in errors[0]


def test_dialog_asks_for_a_destination_before_anything_else(monkeypatch, tmp_path):
    archive = tmp_path / "rof2.zip"
    archive.write_bytes(b"PK")
    screen, _widgets, dismissed, errors = _dialog(
        monkeypatch, provisioning=True, source=str(archive), folder="")

    AddServerScreen._confirm(screen)

    assert not dismissed
    assert errors[0] == "Choose where to create the new EverQuest folder."


def test_browse_mode_payload_is_unchanged_apart_from_its_mode(monkeypatch, tmp_path):
    """The existing add flow has to keep behaving exactly as it did."""
    folder = _eq_folder(tmp_path)
    screen, _widgets, dismissed, errors = _dialog(monkeypatch, folder=str(folder))

    AddServerScreen._confirm(screen)

    assert errors == []
    assert dismissed[0]["mode"] == AddServerScreen.BROWSE
    assert dismissed[0]["eqpath"] == str(folder)
    assert "source" not in dismissed[0]


def test_a_locked_slug_skips_the_must_be_new_check(monkeypatch, tmp_path):
    """The row you clicked already exists; only its folder is missing."""
    folder = _eq_folder(tmp_path)
    screen, _widgets, dismissed, errors = _dialog(
        monkeypatch, folder=str(folder), locked=("thegrind", "The Grind"))

    AddServerScreen._confirm(screen)

    assert errors == []
    assert dismissed[0]["slug"] == "thegrind"
