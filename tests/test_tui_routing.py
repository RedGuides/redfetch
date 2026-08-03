"""Routing tests for the split client/server dropdowns (terminal_ui.py).

The TUI has no full harness; these pin the row builders, the dropdown sync
(visibility included), and the app-level routing handlers with plain stand-in
objects — no App is mounted.
"""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from redfetch import config

# terminal_ui's Redfetch class evaluates config.settings.ENV at import time (a
# reactive default); stand in for it if config isn't initialized, then restore.
_prior_settings = config.settings
if _prior_settings is None:
    config.settings = SimpleNamespace(ENV="LIVE")
try:
    from redfetch import terminal_ui as tui
finally:
    config.settings = _prior_settings


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def fake_app():
    """A stand-in for the Redfetch app with recorded routing calls."""
    calls = []
    app = SimpleNamespace(
        current_env="EMU",
        active_server=None,
        switch_active_server=lambda slug: calls.append(("switch_server", slug)),
        switch_to_bare_setup=lambda env: calls.append(("bare", env)),
        refresh_after_server_change=lambda: calls.append(("refresh",)),
    )
    return app, calls


class FakeSelect:
    def __init__(self):
        self.display = True
        self.server_rows: list = []
        self.value = None

    def replace_options(self, options):
        self.server_rows = options


def _fake_tab(app, select):
    return SimpleNamespace(
        app=app,
        query_one=lambda selector, _type=None: select,
        prevent=lambda *events: nullcontext(),
    )


# --- row builders ------------------------------------------------------------

def test_client_rows_come_from_envs():
    assert tui.build_client_rows() == [
        (label, env) for env, label in config.ENVS.items()
    ]


def test_server_rows_lead_with_the_bare_setup(monkeypatch):
    monkeypatch.setattr(
        tui, "configured_servers", lambda env: [("lazarus", "Project Lazarus")]
    )
    assert tui.build_server_rows("EMU") == [
        (config.BARE_SERVER_LABEL, tui.BARE_SETUP_ID),
        ("Project Lazarus", "lazarus"),
    ]


# --- client select routing ---------------------------------------------------

def test_client_row_switches_client_only(fake_app):
    app, calls = fake_app
    tui.Redfetch.handle_client_selected(app, "TEST")
    assert app.current_env == "TEST"
    assert calls == []  # never a server switch — the un-amended contract


def test_client_row_mount_redelivery_is_noop(fake_app):
    app, calls = fake_app
    tui.Redfetch.handle_client_selected(app, "EMU")
    assert app.current_env == "EMU" and calls == []


# --- server select routing ---------------------------------------------------

def test_bare_row_leaves_the_named_server(fake_app, monkeypatch):
    app, calls = fake_app
    state = {"active": "lazarus"}
    monkeypatch.setattr(tui.servers, "get_active_server", lambda env: state["active"])
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: True)

    def bare(env):
        state["active"] = None
        calls.append(("bare", env))

    app.switch_to_bare_setup = bare
    tui.Redfetch.handle_server_selected(app, tui.BARE_SETUP_ID)
    assert calls == [("bare", "EMU")]


def test_bare_row_mount_redelivery_is_noop(fake_app, monkeypatch):
    app, calls = fake_app
    monkeypatch.setattr(tui.servers, "get_active_server", lambda env: None)
    tui.Redfetch.handle_server_selected(app, tui.BARE_SETUP_ID)
    assert calls == []


def test_bare_row_with_stale_active_server_is_display_only(fake_app, monkeypatch):
    """A stale ACTIVE_SERVER derives to the bare row at mount; the mount-time
    Changed must not 'heal' settings.local.toml by switching unasked."""
    app, calls = fake_app
    monkeypatch.setattr(tui.servers, "get_active_server", lambda env: "ghost")
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: False)
    tui.Redfetch.handle_server_selected(app, tui.BARE_SETUP_ID)
    assert calls == []


def test_blocked_bare_switch_restores_dropdowns(fake_app, monkeypatch):
    app, calls = fake_app
    monkeypatch.setattr(tui.servers, "get_active_server", lambda env: "lazarus")
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: True)
    tui.Redfetch.handle_server_selected(app, tui.BARE_SETUP_ID)
    assert calls == [("bare", "EMU"), ("refresh",)]


def test_slug_row_switches_server(fake_app):
    app, calls = fake_app

    def switch(slug):
        app.active_server = slug
        calls.append(("switch_server", slug))

    app.switch_active_server = switch
    tui.Redfetch.handle_server_selected(app, "lazarus")
    assert calls == [("switch_server", "lazarus")]


def test_blocked_server_switch_restores_dropdowns(fake_app):
    app, calls = fake_app  # the stub switch never sets active_server
    tui.Redfetch.handle_server_selected(app, "lazarus")
    assert calls == [("switch_server", "lazarus"), ("refresh",)]


# --- dropdown sync -----------------------------------------------------------

def test_server_select_hidden_on_single_server_client(fake_app):
    app, _ = fake_app
    app.current_env = "LIVE"
    select = FakeSelect()
    tui.sync_server_select(_fake_tab(app, select), "server_select")
    assert select.display is False


def test_server_select_shown_and_synced_on_emu(fake_app, monkeypatch):
    app, _ = fake_app
    app.active_server = "lazarus"
    monkeypatch.setattr(
        tui, "configured_servers", lambda env: [("lazarus", "Project Lazarus")]
    )
    select = FakeSelect()
    tui.sync_server_select(_fake_tab(app, select), "server_select")
    assert select.display is True
    assert select.server_rows == [
        (config.BARE_SERVER_LABEL, tui.BARE_SETUP_ID),
        ("Project Lazarus", "lazarus"),
    ]
    assert select.value == "lazarus"


def test_stale_active_server_falls_back_to_bare(fake_app, monkeypatch):
    """A hand-edited ACTIVE_SERVER naming an unconfigured slug can't crash the Select."""
    app, _ = fake_app
    app.active_server = "ghost"
    monkeypatch.setattr(tui, "configured_servers", lambda env: [])
    select = FakeSelect()
    tui.sync_server_select(_fake_tab(app, select), "server_select")
    assert select.value == tui.BARE_SETUP_ID
