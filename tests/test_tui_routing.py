"""Routing tests for the split client/server dropdowns (tui.py).

The TUI has no full harness; these pin the row builders, the dropdown sync
(visibility included), and the app-level routing handlers with plain stand-in
objects — no App is mounted.
"""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from rich.text import Text
from textual.content import Content
from textual.widgets.option_list import Option

from redfetch import config
from redfetch import tui_servers
from redfetch import tui_settings
from redfetch import tui_shortcuts
from redfetch import tui_widgets

from conftest import FakeInput, _eq_folder
from test_laa import _eq, _pe_bytes

# tui's Redfetch class evaluates config.settings.ENV at import time (a
# reactive default); stand in for it if config isn't initialized, then restore.
_prior_settings = config.settings
if _prior_settings is None:
    config.settings = SimpleNamespace(ENV="LIVE")
try:
    from redfetch import tui
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
    assert tui_widgets.build_client_rows() == [
        (label, env) for env, label in config.ENVS.items()
    ]


def test_server_rows_lead_with_the_bare_setup(monkeypatch):
    monkeypatch.setattr(
        tui_widgets, "configured_servers", lambda env: [("lazarus", "Project Lazarus")]
    )
    rows = tui_widgets.build_server_rows("EMU")
    assert rows[0] == (config.BARE_SERVER_LABEL, tui.BARE_SETUP_ID)
    assert [(str(prompt), value) for prompt, value in rows[1:]] == [
        ("Project Lazarus", "lazarus"),
    ]


def test_server_rows_keep_a_bracketed_label_literal(monkeypatch):
    """'[TAKP]' is a real emu server's name; escape() would have eaten it."""
    monkeypatch.setattr(tui_widgets, "configured_servers", lambda env: [("peq", "PEQ [TAKP]")])
    prompt, _value = tui_widgets.build_server_rows("EMU")[1]
    assert prompt.plain == "PEQ [TAKP]"
    # A Text prompt (not Content) is what keeps Select's type-to-search working.
    assert isinstance(prompt, Text)


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


# --- "Get patcher" button gate -------------------------------------------------

class FakeButton:
    def __init__(self):
        self.disabled = False
        self.display = True
        self._label = Content("")
        self._tooltip = Content("")

    # Mirrors Button.validate_label, so assigning a str really does parse markup
    # here too — otherwise the label tests would pass on a sink that can't lose text.
    @property
    def label(self) -> Content:
        return self._label

    @label.setter
    def label(self, value) -> None:
        self._label = Content.from_text(value)

    # Mirrors the Tooltip Static: a str parses markup, a Content passes through.
    @property
    def tooltip(self) -> Content:
        return self._tooltip

    @tooltip.setter
    def tooltip(self, value) -> None:
        self._tooltip = Content.from_markup(value) if isinstance(value, str) else value


def _patcher_button(monkeypatch, tmp_path, *, slug, selected_active, context, listed=None,
                    running=False, enabling=False):
    """Drive ServersTab._refresh_patcher_button with stand-ins; real patcher gates."""
    button = FakeButton()
    tab = SimpleNamespace(
        app=SimpleNamespace(patcher_install_running=running, laa_enable_running=enabling,
                            provision_running=False),
        query_one=lambda selector, _type=None: button,
    )
    monkeypatch.setattr(tui.servers, "active_server_context", lambda env: context)
    monkeypatch.setattr(tui.servers, "list_servers", lambda env: listed or {})
    tui_servers.ServersTab._refresh_patcher_button(tab, "EMU", slug, selected_active)
    return button


def _context(tmp_path, **kwargs):
    fields = dict(
        label="Project Lazarus", eqpath=str(tmp_path),
        patcher_url="https://laz.example.test/p.zip", patcher_exe="LazarusPatcherCLI.exe",
    )
    return tui.servers.ServerContext(**{**fields, **kwargs})


def test_patcher_button_enabled_for_the_active_server(monkeypatch, tmp_path):
    button = _patcher_button(
        monkeypatch, tmp_path, slug="lazarus", selected_active=True, context=_context(tmp_path)
    )
    assert button.disabled is False
    assert "Download the Project Lazarus patcher" in button.tooltip.plain


def test_patcher_button_disabled_once_installed(monkeypatch, tmp_path):
    (tmp_path / "LazarusPatcherCLI.exe").write_bytes(b"MZ")
    button = _patcher_button(
        monkeypatch, tmp_path, slug="lazarus", selected_active=True, context=_context(tmp_path)
    )
    assert button.disabled is True
    assert "already" in button.tooltip.plain


def test_patcher_button_sends_you_to_switch_first(monkeypatch, tmp_path):
    """An inactive row's folder lives in its snapshot, so bootstrap can't act on it."""
    button = _patcher_button(
        monkeypatch, tmp_path, slug="other", selected_active=False, context=_context(tmp_path),
        listed={"other": {"patcher_url": "https://other.example.test/p.zip"}},
    )
    assert button.disabled is True
    assert "Switch to this server first" in button.tooltip.plain


def test_patcher_button_disabled_on_the_bare_setup(monkeypatch, tmp_path):
    """No entry behind it, so no patcher_url — the data hides the action, not a branch."""
    bare = _context(tmp_path, label=config.BARE_SERVER_LABEL,
                    patcher_url="", patcher_exe="")
    button = _patcher_button(
        monkeypatch, tmp_path, slug=tui.BARE_SETUP_ID, selected_active=True, context=bare
    )
    assert button.disabled is True
    assert "no patcher" in button.tooltip.plain


def test_patcher_button_disabled_while_downloading(monkeypatch, tmp_path):
    button = _patcher_button(
        monkeypatch, tmp_path, slug="lazarus", selected_active=True,
        context=_context(tmp_path), running=True,
    )
    assert button.disabled is True
    assert "Downloading" in button.tooltip.plain


def test_patcher_button_keeps_free_text_labels_literal(monkeypatch, tmp_path):
    """Tooltips are a markup sink, and escape() misses any tag that isn't [a-z#/@]-initial.

    'PEQ [TAKP]' is a real emu server's name: escape() leaves it untouched and the
    tooltip then renders 'PEQ ' — silent text loss, no exception.
    """
    button = _patcher_button(
        monkeypatch, tmp_path, slug="lazarus", selected_active=True,
        context=_context(tmp_path, label="PEQ [TAKP]"),
    )
    assert button.disabled is False
    assert button.tooltip.plain == (
        "Download the PEQ [TAKP] patcher into its EverQuest folder."
    )


def test_patcher_button_names_the_missing_exe_on_the_active_server(monkeypatch, tmp_path):
    """A hand-edited URL-only entry must not tell an active user to switch."""
    half = _context(tmp_path, patcher_exe="")
    button = _patcher_button(
        monkeypatch, tmp_path, slug="lazarus", selected_active=True, context=half,
        listed={"lazarus": {"patcher_url": "https://laz.example.test/p.zip"}},
    )
    assert button.disabled is True
    assert "patcher_exe" in button.tooltip.plain


@pytest.mark.parametrize(
    "flag", ["patcher_install_running", "laa_enable_running", "provision_running"])
def test_install_active_patcher_refuses_while_busy(flag):
    """A second press must not cancel-and-restart the worker, and the payload move
    must not race an LAA rewrite of eqgame.exe."""
    calls = []
    fields = dict(
        is_updating=False, interface_running=False, patcher_install_running=False,
        laa_enable_running=False, provision_running=False, current_env="EMU",
        notify=lambda *a, **k: calls.append("notify"),
        _install_patcher_worker=lambda ctx: calls.append("worker"),
    )
    fields[flag] = True
    app = SimpleNamespace(**fields)
    assert tui.Redfetch.install_active_patcher(app) is False
    assert calls == []


def test_install_active_patcher_starts_and_latches(monkeypatch, tmp_path):
    started = []
    app = SimpleNamespace(
        is_updating=False, interface_running=False, patcher_install_running=False,
        laa_enable_running=False, provision_running=False, current_env="EMU",
        notify=lambda *a, **k: None,
        _install_patcher_worker=lambda ctx: started.append(ctx.label),
    )
    monkeypatch.setattr(tui.servers, "active_server_context", lambda env: _context(tmp_path))

    assert tui.Redfetch.install_active_patcher(app) is True

    assert app.patcher_install_running is True  # the reentry latch
    assert started == ["Project Lazarus"]


# --- AddServerScreen: the custom patcher pair --------------------------------

def _confirm_custom(monkeypatch, tmp_path, *, url="", exe="", folder=None):
    """Drive AddServerScreen._confirm as a custom add with stand-ins; real patcher and
    eqgame.exe gates. The default folder is a real EverQuest folder, so it passes."""
    monkeypatch.setattr(tui.servers, "validate_server_slug",
                        lambda slug, must_be_new=False: slug)
    errors = []
    dismissed = []
    widgets = {
        "#server_dialog_error": SimpleNamespace(
            update=lambda msg: errors.append(getattr(msg, "plain", None) or str(msg))),
        "#add_folder": FakeInput(str(_eq_folder(tmp_path)) if folder is None else folder),
        "#add_slug": FakeInput("myserver"),
        "#add_label": FakeInput("My Server"),
        "#add_patcher_url": FakeInput(url),
        "#add_patcher_exe": FakeInput(exe),
    }
    screen = SimpleNamespace(
        query_one=lambda selector, _type=None: widgets[selector],
        _is_custom=lambda: True,
        _is_provision=lambda: False,
        _locked=None,
        BROWSE=tui_servers.AddServerScreen.BROWSE,
        dismiss=lambda payload: dismissed.append(payload),
    )
    tui_servers.AddServerScreen._confirm(screen)
    return dismissed, errors


def test_add_dialog_carries_the_patcher_pair(monkeypatch, tmp_path):
    dismissed, errors = _confirm_custom(
        monkeypatch, tmp_path, url="https://myserver.example/patcher.zip", exe="MyPatcher.exe")
    assert errors == []
    assert dismissed[0]["patcher_url"] == "https://myserver.example/patcher.zip"
    assert dismissed[0]["patcher_exe"] == "MyPatcher.exe"


def test_add_dialog_without_a_patcher_still_adds(monkeypatch, tmp_path):
    dismissed, errors = _confirm_custom(monkeypatch, tmp_path)
    assert errors == []
    assert (dismissed[0]["patcher_url"], dismissed[0]["patcher_exe"]) == ("", "")


def test_add_dialog_requires_the_file_name_with_a_link(monkeypatch, tmp_path):
    """A URL alone can never satisfy has_patcher — refuse it where the user can fix it."""
    dismissed, errors = _confirm_custom(
        monkeypatch, tmp_path, url="https://myserver.example/patcher.zip")
    assert not dismissed
    assert "file name" in errors[0]


def test_add_dialog_requires_the_link_with_a_file_name(monkeypatch, tmp_path):
    dismissed, errors = _confirm_custom(monkeypatch, tmp_path, exe="MyPatcher.exe")
    assert not dismissed
    assert "download link" in errors[0]


def test_add_dialog_rejects_a_hostile_exe_name(monkeypatch, tmp_path):
    """Same gate as bootstrap and run, but the error lands where the user typed it."""
    dismissed, errors = _confirm_custom(
        monkeypatch, tmp_path, url="https://myserver.example/patcher.zip", exe="..\\evil.exe")
    assert not dismissed
    assert "bare" in errors[0]  # validate_patcher_exe's own message


# --- Shortcuts tab: the per-server patcher entry -----------------------------

def _shortcuts_recompute(monkeypatch, runnables):
    """Drive ShortcutsTab._recompute over a controlled registry; real gates."""
    buttons = {}
    tab = SimpleNamespace(
        app=SimpleNamespace(is_updating=False, provision_running=False),
        query_one=lambda selector, _type=None: buttons.setdefault(selector, FakeButton()),
    )
    monkeypatch.setattr(tui.shortcuts, "RUNNABLES", tuple(runnables))
    monkeypatch.setattr(tui.shortcuts, "OPENABLES", ())
    tui_shortcuts.ShortcutsTab._recompute(tab)
    return buttons


def _patcher_runnable(folder, exe, label):
    return tui.shortcuts.Runnable(
        "patcher", "Server patcher", "", lambda: str(folder),
        resolve_executable=lambda: exe, resolve_label=lambda: label,
    )


def test_shortcuts_tab_hides_a_patcher_with_no_server_behind_it(monkeypatch, tmp_path):
    """Live and the bare setup can't have one, so the button isn't there to press."""
    buttons = _shortcuts_recompute(monkeypatch, [_patcher_runnable(tmp_path, "", "")])
    assert buttons["#run_patcher"].display is False


def test_shortcuts_tab_disables_a_patcher_that_isnt_downloaded_yet(monkeypatch, tmp_path):
    """Shown but dead: 'not bootstrapped yet' is exactly what unavailable means here."""
    buttons = _shortcuts_recompute(
        monkeypatch, [_patcher_runnable(tmp_path, "LazarusPatcherCLI.exe", "Lazarus patcher")]
    )
    assert buttons["#run_patcher"].display is True
    assert buttons["#run_patcher"].disabled is True


def test_shortcuts_tab_enables_the_installed_patcher_and_keeps_its_label_literal(monkeypatch, tmp_path):
    """Button labels parse markup, and rich's escape() is too narrow to stop it.

    'PEQ [TAKP]' is a realistic server name whose tag is uppercase-initial, so
    rich.markup.escape leaves it alone and textual then eats it. Content does not.
    """
    (tmp_path / "LazarusPatcherCLI.exe").write_bytes(b"MZ")
    buttons = _shortcuts_recompute(
        monkeypatch,
        [_patcher_runnable(tmp_path, "LazarusPatcherCLI.exe", "PEQ [TAKP] patcher")],
    )
    button = buttons["#run_patcher"]
    assert button.display is True and button.disabled is False
    assert button.label.plain == "PEQ [TAKP] patcher"


def test_shortcuts_tab_keeps_static_entries_visible_when_missing(monkeypatch, tmp_path):
    static = tui.shortcuts.Runnable("eqbcs", "EQBCS 💬", "EQBCS.exe", lambda: str(tmp_path))
    buttons = _shortcuts_recompute(monkeypatch, [static])
    assert buttons["#run_eqbcs"].display is True     # never hidden...
    assert buttons["#run_eqbcs"].disabled is True    # ...just dead until it's installed
    assert buttons["#run_eqbcs"].label == "EQBCS 💬"


def test_shortcuts_tab_reconditions_on_a_server_switch():
    """Two servers can share an EQ folder, so eq_path alone misses the switch."""
    watched = []
    tab = SimpleNamespace(
        app=SimpleNamespace(),
        watch=lambda obj, attr, callback: watched.append(attr),
        _recompute=lambda: None,
    )
    tui_shortcuts.ShortcutsTab.on_mount(tab)
    assert "active_server" in watched


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
    tui_widgets.sync_server_select(_fake_tab(app, select), "server_select")
    assert select.display is False


def test_server_select_shown_and_synced_on_emu(fake_app, monkeypatch):
    app, _ = fake_app
    app.active_server = "lazarus"
    monkeypatch.setattr(
        tui_widgets, "configured_servers", lambda env: [("lazarus", "Project Lazarus")]
    )
    select = FakeSelect()
    tui_widgets.sync_server_select(_fake_tab(app, select), "server_select")
    assert select.display is True
    assert [(str(prompt), value) for prompt, value in select.server_rows] == [
        (config.BARE_SERVER_LABEL, tui.BARE_SETUP_ID),
        ("Project Lazarus", "lazarus"),
    ]
    assert select.value == "lazarus"


def test_stale_active_server_falls_back_to_bare(fake_app, monkeypatch):
    """A hand-edited ACTIVE_SERVER naming an unconfigured slug can't crash the Select."""
    app, _ = fake_app
    app.active_server = "ghost"
    monkeypatch.setattr(tui_widgets, "configured_servers", lambda env: [])
    select = FakeSelect()
    tui_widgets.sync_server_select(_fake_tab(app, select), "server_select")
    assert select.value == tui.BARE_SETUP_ID


# --- the eqhost.txt shortcut on the Servers tab ------------------------------

LAZ = "login.eqemulator.net:5999"


def _run_patcher_button(monkeypatch, *, selected_active, context):
    """Drive ServersTab._refresh_run_patcher_button against the real shortcuts registry."""
    button = FakeButton()
    tab = SimpleNamespace(
        app=SimpleNamespace(provision_running=False, patcher_install_running=False,
                            laa_enable_running=False),
        query_one=lambda selector, _type=None: button,
    )
    monkeypatch.setattr(tui_servers.shortcuts, "_active_context", lambda: context)
    monkeypatch.setattr(tui.servers, "active_server_context", lambda env: context)
    tui_servers.ServersTab._refresh_run_patcher_button(tab, "EMU", selected_active)
    return button


def test_run_patcher_button_takes_the_resolved_label(monkeypatch, tmp_path):
    """The tab has to re-resolve it like the Shortcuts tab; compose's is only a fallback."""
    button = _run_patcher_button(
        monkeypatch, selected_active=True, context=_context(tmp_path, label="PEQ [TAKP]"))

    assert button.label.plain == "PEQ [TAKP] patcher 🩹"


def _eqhost_button(monkeypatch, *, selected_active, eq_dir=None, provisioning=False):
    """Drive ServersTab._refresh_eqhost_button against the real shortcuts registry."""
    button = FakeButton()
    tab = SimpleNamespace(
        app=SimpleNamespace(provision_running=provisioning),
        query_one=lambda selector, _type=None: button,
    )
    # The registry captured _eq_dir at import, so the folder has to arrive through config.
    monkeypatch.setattr(tui_servers.shortcuts.config, "active_settings",
                        lambda: {"EQPATH": eq_dir or ""})
    tui_servers.ServersTab._refresh_eqhost_button(tab, selected_active)
    return button


def _with_eqhost(tmp_path, host=LAZ):
    folder = _eq_folder(tmp_path)
    (folder / "eqhost.txt").write_text(
        f"[LoginServer]\nHost={host}\n", encoding="utf-8")
    return folder


def test_eqhost_button_sends_you_to_switch_first(monkeypatch, tmp_path):
    """The shortcut always resolves the active server, never the highlighted one."""
    button = _eqhost_button(
        monkeypatch, selected_active=False, eq_dir=str(_with_eqhost(tmp_path)))
    assert button.disabled is True
    assert "Switch to this server first" in button.tooltip.plain


def test_eqhost_button_names_the_login_server_in_use(monkeypatch, tmp_path):
    button = _eqhost_button(
        monkeypatch, selected_active=True, eq_dir=str(_with_eqhost(tmp_path)))
    assert button.disabled is False
    assert LAZ in button.tooltip.plain


def test_eqhost_button_is_dead_without_a_file_to_open(monkeypatch, tmp_path):
    button = _eqhost_button(
        monkeypatch, selected_active=True, eq_dir=str(_eq_folder(tmp_path)))
    assert button.disabled is True
    assert "No eqhost.txt" in button.tooltip.plain


def test_eqhost_button_waits_for_a_folder(monkeypatch, tmp_path):
    button = _eqhost_button(monkeypatch, selected_active=True, eq_dir=None)
    assert button.disabled is True


def test_eqhost_button_waits_out_a_provision(monkeypatch, tmp_path):
    button = _eqhost_button(
        monkeypatch, selected_active=True, eq_dir=str(_with_eqhost(tmp_path)),
        provisioning=True)
    assert button.disabled is True


def test_eqhost_button_keeps_free_text_literal(monkeypatch, tmp_path):
    """A hand-edited Host line reaches the tooltip verbatim, brackets and all."""
    button = _eqhost_button(
        monkeypatch, selected_active=True,
        eq_dir=str(_with_eqhost(tmp_path, host="[TAKP]:5999")))
    assert "[TAKP]:5999" in button.tooltip.plain


def test_pressing_the_eqhost_button_opens_the_shortcut():
    """The Servers tab hands the app the same entry the Shortcuts tab does."""
    opened = []
    tab = SimpleNamespace(app=SimpleNamespace(open_target=opened.append))
    tui_servers.ServersTab.handle_eqhost_pressed(tab, None)
    assert opened == [tui_servers.shortcuts.find_openable("eqhost")]


def _write_app(monkeypatch, context, **flags):
    notices = []
    fields = dict(
        is_updating=False, interface_running=False, patcher_install_running=False,
        provision_running=False, current_env="EMU",
        notify=lambda msg, **kw: notices.append((str(msg), kw)),
    )
    monkeypatch.setattr(tui.servers, "active_server_context", lambda env: context)
    return SimpleNamespace(**{**fields, **flags}), notices


# --- the 4GB button ---


def _laa_folder(tmp_path, *, laa_on=False):
    return _eq(tmp_path, body=_pe_bytes(laa_on=laa_on))


def _laa_button(monkeypatch, *, selected_active, context, running=False, enabling=False):
    """Drive ServersTab._refresh_laa_button with stand-ins; the real laa gates."""
    button = FakeButton()
    tab = SimpleNamespace(
        app=SimpleNamespace(patcher_install_running=running, laa_enable_running=enabling,
                            provision_running=False),
        query_one=lambda selector, _type=None: button,
    )
    monkeypatch.setattr(tui.servers, "active_server_context", lambda env: context)
    tui_servers.ServersTab._refresh_laa_button(tab, "EMU", selected_active)
    return button


def test_laa_button_sends_you_to_switch_first(monkeypatch, tmp_path):
    button = _laa_button(
        monkeypatch, selected_active=False, context=_context(tmp_path))
    assert button.disabled is True
    assert "Switch to this server first" in button.tooltip.plain


def test_laa_button_waits_for_a_folder(monkeypatch, tmp_path):
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=""))
    assert button.disabled is True
    assert "folder first" in button.tooltip.plain


def test_laa_button_names_a_folder_that_isnt_everquest(monkeypatch, tmp_path):
    plain = tmp_path / "not-eq"
    plain.mkdir()
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(plain)))
    assert button.disabled is True
    assert "eqgame.exe" in button.tooltip.plain


def test_laa_button_blocks_on_an_exe_that_isnt_a_program(monkeypatch, tmp_path):
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(_eq_folder(tmp_path))))
    assert button.disabled is True
    assert "Windows program" in button.tooltip.plain


def test_laa_button_settles_when_the_flag_is_already_on(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path, laa_on=True)
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(folder)))
    assert button.disabled is True
    assert "already use 4GB" in button.tooltip.plain


def test_laa_button_offers_the_change(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path)
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(folder)))
    assert button.disabled is False
    assert "4GB" in button.tooltip.plain and "eqgame.exe.bak" in button.tooltip.plain


def test_laa_button_works_for_the_bare_setup(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path)
    context = tui.servers.ServerContext(label="Any emu server", eqpath=str(folder))
    button = _laa_button(monkeypatch, selected_active=True, context=context)
    assert button.disabled is False


def test_laa_button_waits_out_a_patcher_download(monkeypatch, tmp_path):
    """The patcher payload can carry a fresh eqgame.exe."""
    folder = _laa_folder(tmp_path)
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(folder)), running=True)
    assert button.disabled is True
    assert "patcher download" in button.tooltip.plain


def test_laa_button_waits_out_its_own_run(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path)
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(folder)), enabling=True)
    assert button.disabled is True
    assert "Setting the 4GB flag" in button.tooltip.plain


def test_laa_button_keeps_free_text_literal(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path)
    button = _laa_button(
        monkeypatch, selected_active=True,
        context=_context(tmp_path, eqpath=str(folder), label="PEQ [TAKP]"))
    assert "PEQ [TAKP]" in button.tooltip.plain


def _laa_app(monkeypatch, context, **flags):
    runs = []
    laa_fields = dict(
        laa_enable_running=False,
        _enable_laa_worker=lambda ctx: runs.append(ctx.label),
    )
    app, notices = _write_app(monkeypatch, context, **{**laa_fields, **flags})
    return app, notices, runs


def test_enable_active_laa_starts_and_latches(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path)
    app, notices, runs = _laa_app(monkeypatch, _context(tmp_path, eqpath=str(folder)))

    assert tui.Redfetch.enable_active_laa(app) is True

    assert app.laa_enable_running is True  # the reentry latch
    assert runs == ["Project Lazarus"]


@pytest.mark.parametrize(
    "flag",
    ["is_updating", "interface_running", "patcher_install_running", "laa_enable_running"],
)
def test_enable_active_laa_refuses_while_busy(monkeypatch, tmp_path, flag):
    folder = _laa_folder(tmp_path)
    app, notices, runs = _laa_app(
        monkeypatch, _context(tmp_path, eqpath=str(folder)), **{flag: True})

    assert tui.Redfetch.enable_active_laa(app) is False

    assert runs == [] and notices == []


def test_enable_active_laa_declines_when_the_flag_is_on(monkeypatch, tmp_path):
    folder = _laa_folder(tmp_path, laa_on=True)
    app, notices, runs = _laa_app(monkeypatch, _context(tmp_path, eqpath=str(folder)))

    assert tui.Redfetch.enable_active_laa(app) is False

    assert runs == []


def test_patcher_button_waits_out_a_laa_change(monkeypatch, tmp_path):
    """Mirror of the 4GB button's courtesy: both rewrite eqgame.exe."""
    button = _patcher_button(
        monkeypatch, tmp_path, slug="lazarus", selected_active=True,
        context=_context(tmp_path), enabling=True)
    assert button.disabled is True
    assert "4GB memory" in button.tooltip.plain


def test_laa_worker_resets_the_latch_and_recomputes(monkeypatch, tmp_path):
    """The finally must clear the latch, or one click disables the buttons for good."""
    recomputes = []
    screen = SimpleNamespace(query_one=lambda sel: SimpleNamespace(
        _recompute=lambda: recomputes.append("servers")))
    app = SimpleNamespace(
        laa_enable_running=True,
        notify=lambda *a, **k: None,
        _base_main_screen=lambda: screen,
    )
    monkeypatch.setattr(tui.laa, "enable", lambda path: None)

    body = tui.Redfetch._enable_laa_worker.__wrapped__
    assert asyncio.run(body(app, _context(tmp_path))) is True

    assert app.laa_enable_running is False
    assert recomputes == ["servers"]


def test_laa_worker_reports_failure_and_still_resets(monkeypatch, tmp_path):
    """LaaError messages carry free-text paths, so the notify must skip markup."""
    notices = []
    app = SimpleNamespace(
        laa_enable_running=True,
        notify=lambda msg, **kw: notices.append((str(msg), kw)),
        _base_main_screen=lambda: None,
    )

    def refuse(path):
        raise tui.laa.LaaError("No eqgame.exe in EQ [TAKP], so there's no flag to set.")

    monkeypatch.setattr(tui.laa, "enable", refuse)

    body = tui.Redfetch._enable_laa_worker.__wrapped__
    assert asyncio.run(body(app, _context(tmp_path))) is False

    assert app.laa_enable_running is False
    message, kwargs = notices[0]
    assert "EQ [TAKP]" in message
    assert kwargs == {"severity": "error", "markup": False}


# --- the Servers tab's folder box, and the eqgame.exe gate on add (P7) ---------


def test_add_dialog_refuses_a_folder_that_isnt_everquest(monkeypatch, tmp_path):
    """Nothing else catches it: the entry saves, then maps, the patcher bootstrap and
    every shortcut quietly resolve against a folder with no game in it."""
    plain = tmp_path / "not-eq"
    plain.mkdir()
    dismissed, errors = _confirm_custom(monkeypatch, tmp_path, folder=str(plain))
    assert not dismissed
    assert "eqgame.exe" in errors[0]


def test_add_dialog_still_asks_for_a_folder_first(monkeypatch, tmp_path):
    """An empty box keeps its own message; the eqgame.exe check can't shadow it."""
    dismissed, errors = _confirm_custom(monkeypatch, tmp_path, folder="")
    assert not dismissed
    assert errors[0] == "Choose the EverQuest folder for this server."


def _configure(monkeypatch, result_payload, *, slug="lazarus"):
    """Drive ServersTab._configure_server's dialog callback.

    The dialog itself is stood in for -- a real AddServerScreen wants a running app,
    and the callback is the whole subject here.
    """
    added, switched, notices, callbacks = [], [], [], []
    provisioned = []
    # The real dialog is only constructed here, never mounted; push_screen is the seam.
    monkeypatch.setattr(tui.servers, "server_label", lambda slug, env: "Project Lazarus")
    monkeypatch.setattr(tui.servers, "add_server",
                        lambda slug, **kwargs: added.append((slug, kwargs)))
    tab = SimpleNamespace(
        app=SimpleNamespace(
            current_env="EMU",
            notify=lambda msg, **kw: notices.append((str(msg), kw)),
            push_screen=lambda screen, callback=None: callbacks.append(callback),
            refresh_after_server_change=lambda: None,
            switch_active_server=lambda slug: switched.append(slug),
            provision_server=lambda payload, switch_after=False: provisioned.append(
                (payload, switch_after)),
        ),
        _busy=lambda: False,
    )
    tui_servers.ServersTab._configure_server(tab, slug, switch_after=True)
    callbacks[0](result_payload)
    return SimpleNamespace(added=added, switched=switched,
                           notices=notices, provisioned=provisioned)


def _browse_payload(folder):
    return {"mode": tui_servers.AddServerScreen.BROWSE, "eqpath": str(folder)}


def test_configure_server_adds_the_folder_the_dialog_returned(monkeypatch, tmp_path):
    """The other add site: clicking an unconfigured row opens the same dialog."""
    folder = _eq_folder(tmp_path)
    result = _configure(monkeypatch, _browse_payload(folder))
    assert result.added == [("lazarus", {"env": "EMU", "eqpath": str(folder)})]
    assert result.switched == ["lazarus"]


def test_configure_server_routes_a_provision_to_the_worker(monkeypatch, tmp_path):
    """Provision mode registers at the end of its own chain, never here."""
    payload = {"mode": "provision", "slug": "lazarus",
               "source": "D:/rof2.zip", "destination": str(tmp_path / "new")}
    result = _configure(monkeypatch, payload)
    assert result.added == [] and result.switched == []
    # switch_after rides through, so the row you clicked still becomes active.
    assert result.provisioned == [(payload, True)]


def test_configure_server_ignores_a_cancelled_dialog(monkeypatch):
    result = _configure(monkeypatch, None)
    assert result.added == [] and result.provisioned == []


def _folder_input_app(monkeypatch, updates, notices):
    """A Redfetch stand-in for handle_input_update, with the maps Select it reaches for."""
    maps = SimpleNamespace(disabled=True, value=None)
    monkeypatch.setattr(tui.config, "update_setting",
                        lambda keys, value, env=None: updates.append((keys, value, env)))
    return SimpleNamespace(
        current_env="EMU", eq_path="",
        notify=lambda msg, **kw: notices.append(str(msg)),
        get_current_eq_maps_value=lambda: "brewall",
        _get_main_screen=lambda: SimpleNamespace(query_one=lambda sel, _type=None: maps),
        _queue_signature_reconcile=lambda: None,
    ), maps


def test_servers_tab_folder_input_shares_the_settings_branch(monkeypatch, tmp_path):
    """One branch for both copies of the setting, so the write and the gate come free."""
    folder = _eq_folder(tmp_path)
    updates, notices = [], []
    app, maps = _folder_input_app(monkeypatch, updates, notices)

    tui.Redfetch.handle_input_update(app, "server_eq_path_input", str(folder))

    assert updates == [(["EQPATH"], str(folder), "EMU")]
    assert app.eq_path == str(folder)
    assert maps.disabled is False


def test_servers_tab_folder_input_refuses_a_folder_without_eqgame(monkeypatch, tmp_path):
    plain = tmp_path / "not-eq"
    plain.mkdir()
    updates, notices = [], []
    app, _maps = _folder_input_app(monkeypatch, updates, notices)

    tui.Redfetch.handle_input_update(app, "server_eq_path_input", str(plain))

    assert updates == [] and app.eq_path == ""
    assert "eqgame.exe" in notices[0]


def test_servers_tab_syncs_the_folder_input(monkeypatch):
    """The box and the active row's own text both read app.eq_path, so they can't drift."""
    tab, widgets = _servers_tab(eq_path="D:/EQ-Laz")
    monkeypatch.setattr(tui.servers, "is_multi_server", lambda env: True)
    monkeypatch.setattr(tui.utils, "dx9_missing", lambda: False)

    tui_servers.ServersTab._recompute(tab)

    assert widgets["#server_eq_path_input"].value == "D:/EQ-Laz"


def _servers_tab(*, eq_path="", provisioning=False, cancellable=False):
    """A ServersTab stand-in carrying the widgets _recompute reaches for."""
    widgets = {
        "#dx9_notice": SimpleNamespace(display=False),
        "#server_eq_path_input": FakeInput("stale"),
        "#server_provision_row": SimpleNamespace(
            classes=set(),
            set_class=lambda add, name, _w=None: None,
        ),
        "#provision_cancel": FakeButton(),
    }
    row = widgets["#server_provision_row"]
    row.set_class = lambda add, name: (
        row.classes.add(name) if add else row.classes.discard(name)
    )
    tab = SimpleNamespace(
        app=SimpleNamespace(is_updating=False, interface_running=False,
                            current_env="EMU", eq_path=eq_path,
                            provision_running=provisioning,
                            provision_cancellable=cancellable),
        query_one=lambda selector, _type=None: widgets[selector],
        _rebuild_list=lambda: None,
        _refresh_buttons=lambda: None,
        disabled=False,
    )
    return tab, widgets


def test_provision_row_hides_unless_a_provision_is_running(monkeypatch):
    monkeypatch.setattr(tui.servers, "is_multi_server", lambda env: True)
    monkeypatch.setattr(tui.utils, "dx9_missing", lambda: False)

    idle, idle_widgets = _servers_tab()
    tui_servers.ServersTab._recompute(idle)
    assert "hidden" in idle_widgets["#server_provision_row"].classes

    busy, busy_widgets = _servers_tab(provisioning=True, cancellable=True)
    tui_servers.ServersTab._recompute(busy)
    assert "hidden" not in busy_widgets["#server_provision_row"].classes
    assert busy_widgets["#provision_cancel"].disabled is False


def test_provisioning_never_disables_the_whole_tab(monkeypatch):
    """The tab-wide disable would grey out the provision's own Cancel button."""
    monkeypatch.setattr(tui.servers, "is_multi_server", lambda env: True)
    monkeypatch.setattr(tui.utils, "dx9_missing", lambda: False)

    tab, _widgets = _servers_tab(provisioning=True, cancellable=True)
    tui_servers.ServersTab._recompute(tab)

    assert tab.disabled is False


def test_cancel_greys_once_the_folder_has_landed(monkeypatch):
    """Past the move the chain registers regardless, so there's nothing left to stop."""
    monkeypatch.setattr(tui.servers, "is_multi_server", lambda env: True)
    monkeypatch.setattr(tui.utils, "dx9_missing", lambda: False)

    tab, widgets = _servers_tab(provisioning=True, cancellable=False)
    tui_servers.ServersTab._recompute(tab)

    assert widgets["#provision_cancel"].disabled is True


# --- which row the server list opens on ---


class FakeOptionList:
    """The slice of OptionList _rebuild_list drives."""

    def __init__(self, options=(), highlighted=None):
        self.options = list(options)
        self.highlighted = highlighted

    def clear_options(self):
        self.options.clear()
        self.highlighted = None

    def add_option(self, option):
        self.options.append(option)

    def get_option_at_index(self, index):
        return self.options[index]


def _rebuild(monkeypatch, *, active, option_list=None):
    listed = {
        "aporia": {"label": "Aporia", "eqpath": "D:/EQ-Ap"},
        "lazarus": {"label": "Project Lazarus", "eqpath": "D:/EQ-Laz"},
    }
    monkeypatch.setattr(tui.servers, "list_servers", lambda env: listed)
    monkeypatch.setattr(tui.servers, "get_active_server", lambda env: active)
    monkeypatch.setattr(tui.servers, "is_server_configured", lambda slug, env: True)
    monkeypatch.setattr(tui.servers, "generic_eqpath", lambda env: "D:/EQ")
    option_list = option_list if option_list is not None else FakeOptionList()
    tab = SimpleNamespace(
        app=SimpleNamespace(current_env="EMU", eq_path="D:/EQ-Laz"),
        query_one=lambda selector, _type=None: option_list,
    )
    tab._highlighted_slug = lambda: tui_servers.ServersTab._highlighted_slug(tab)
    tui_servers.ServersTab._rebuild_list(tab)
    return option_list


def test_first_build_highlights_the_active_server(monkeypatch):
    """Nothing to preserve yet, so the list opens on the server you're on, not row 0."""
    option_list = _rebuild(monkeypatch, active="lazarus")

    assert option_list.get_option_at_index(option_list.highlighted).id == "lazarus"


def test_first_build_lands_on_the_bare_setup_when_nothing_is_active(monkeypatch):
    option_list = _rebuild(monkeypatch, active=None)

    assert option_list.get_option_at_index(option_list.highlighted).id == tui.BARE_SETUP_ID


def test_settings_recompute_waits_out_a_recompose():
    """Resetting the active server queues a pass that can land while the tab is empty --
    a recompose removes its children first, and the screen's pump runs meanwhile."""
    queued = []
    tab = SimpleNamespace(
        app=SimpleNamespace(active_server=None),
        query=lambda selector: [],
        call_after_refresh=queued.append,
    )
    tab._recompute = lambda: None

    tui_settings.SettingsTab._recompute(tab)  # NoMatches on #vvmq_path_input before

    assert queued == [tab._recompute]


def test_rebuild_keeps_the_row_you_moved_to(monkeypatch):
    """A refresh mid-browse can't yank the highlight back to the active server."""
    browsing = FakeOptionList([Option(Text("Aporia"), id="aporia")], highlighted=0)
    option_list = _rebuild(monkeypatch, active="lazarus", option_list=browsing)

    assert option_list.get_option_at_index(option_list.highlighted).id == "aporia"
