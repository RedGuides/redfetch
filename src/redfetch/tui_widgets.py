"""Widgets and helpers shared by more than one TUI tab."""
# third-party
from rich.text import Text
from textual_fspicker import Filters

# textual framework
from textual.containers import ScrollableContainer
from textual.widgets import RadioButton, RadioSet, Select

# local
from redfetch import config
from redfetch import servers


# Tri-state toggle: No / Ask / Yes maps to config values False / None / True.
TRISTATE_OPTIONS: list[tuple[str, bool | None]] = [
    ("No", False),
    ("Ask", None),
    ("Yes", True),
]


def clean_source_filters() -> Filters:
    """File-picker filters for a zip or iso, folder sources use a directory picker."""
    return Filters(
        ("RoF2 copy (*.zip, *.iso)", lambda p: p.suffix.lower() in (".zip", ".iso")),
        ("All files", lambda _p: True),
    )


def tristate_index(value) -> int:
    """Return the radio index for a stored config value (defaults to Ask)."""
    if value is True:
        return 2
    if value is False:
        return 0
    return 1  # None / unset -> Ask


def tristate_label(value) -> str:
    """Human-readable label for a stored config value."""
    return TRISTATE_OPTIONS[tristate_index(value)][0]


def make_tristate(widget_id: str, value) -> RadioSet:
    """Build a horizontal No/Ask/Yes radio set for ``value``."""
    selected = tristate_index(value)
    return RadioSet(
        *(
            RadioButton(label, value=(i == selected), compact=True)
            for i, (label, _v) in enumerate(TRISTATE_OPTIONS)
        ),
        id=widget_id,
        compact=True,
    )


def set_tristate(radio_set: RadioSet, value) -> None:
    """Select the No/Ask/Yes button matching ``value`` without firing Changed."""
    target = list(radio_set.query(RadioButton))[tristate_index(value)]
    if not target.value:
        target.value = True


class ServerSelect(Select[str]):
    """The server dropdown: the bare setup plus the client's configured servers."""

    def __init__(self, options: list[tuple[str | Text, str]], value: str, widget_id: str) -> None:
        super().__init__(
            options,
            id=widget_id,
            classes="bordertitles",
            value=value,
            prompt="Select server",
            allow_blank=False,
            tooltip=(
                "The emu server you intend to play on."
            ),
        )
        self.server_rows = options

    def replace_options(self, options: list[tuple[str | Text, str]]) -> None:
        """Replace options; callers preserve selection and suppress ``Changed``."""
        self.server_rows = options
        self.set_options(options)


def build_client_rows() -> list[tuple[str, str]]:
    """Client dropdown rows, one per client env."""
    return [(label, env) for env, label in config.ENVS.items()]


def make_client_select(value: str, widget_id: str) -> Select[str]:
    """The client dropdown: Live / Test / Emu (RoF2). Its options never change."""
    return Select[str](
        build_client_rows(),
        id=widget_id,
        classes="bordertitles",
        value=value,
        prompt="Select client",
        allow_blank=False,
        tooltip=(
            "The game client you play. 'Live' is the normal retail client from everquest dot com."
        ),
    )


def configured_servers(env: str) -> list[tuple[str, str]]:
    """(slug, label) for each of the env's servers that's fully set up."""
    return [
        (slug, entry.get("label") or slug)
        for slug, entry in sorted(servers.list_servers(env).items())
        if servers.is_server_configured(slug, env)
    ]


# Server-row id for the bare setup
BARE_SETUP_ID = "#bare"


def build_server_rows(env: str) -> list[tuple[str | Text, str]]:
    """Server dropdown rows: the bare setup first, then configured servers."""
    return [
        (config.BARE_SERVER_LABEL, BARE_SETUP_ID),
        # Must use Text(): escape() only handles [a-z#/@] tags, Content() breaks type-to-search.
        *((Text(label), slug) for slug, label in configured_servers(env)),
    ]


def server_select_state(app) -> tuple[list[tuple[str | Text, str]], str]:
    """The server dropdown's rows and selection for the current client."""
    rows = build_server_rows(app.current_env)
    value = app.active_server or BARE_SETUP_ID
    if value not in {row_value for _, row_value in rows}:
        value = BARE_SETUP_ID
    return rows, value


def sync_client_select(tab: ScrollableContainer, select_id: str) -> None:
    """Point a client dropdown at the current client without emitting ``Changed``."""
    select = tab.query_one(f"#{select_id}", Select)
    with tab.prevent(Select.Changed):
        if select.value != tab.app.current_env:
            select.value = tab.app.current_env


def sync_server_select(tab: ScrollableContainer, select_id: str) -> None:
    """Refresh a server dropdown without emitting ``Changed``."""
    app = tab.app
    select = tab.query_one(f"#{select_id}", ServerSelect)
    show = servers.is_multi_server(app.current_env)
    select.display = show
    if not show:
        return
    rows, value = server_select_state(app)
    with tab.prevent(Select.Changed):
        if select.server_rows != rows:
            select.replace_options(rows)
        if select.value != value:
            select.value = value
