"""The Account tab: who you're logged in as, and what that gets you."""
# textual framework
from textual import on
from textual.app import ComposeResult
from textual.containers import Center, ScrollableContainer
from textual.widgets import Button, Label


class AccountTab(ScrollableContainer):
    """Content for the Account tab."""

    def compose(self) -> ComposeResult:
        with Center():
            yield Label("Loading...", id="account_label")
        with Center():
            yield Button(
                "Ding for level 2 🆙",
                id="btn_ding",
                variant="primary",
                tooltip="Upgrade your RedGuides account to level 2.",
            )
            yield Button(
                "Manage Watched Resources 👀",
                id="btn_watched",
                variant="default",
                classes="web_link",
                tooltip="Manage the resources you're watching.",
            )
            yield Button(
                "Licensed Resources 🎫",
                id="btn_licensed",
                variant="default",
                classes="web_link",
                tooltip="Manage your purchased resources.",
            )
            yield Button(
                "Manage Account 🧾",
                id="btn_account",
                variant="default",
                classes="web_link",
                tooltip="Manage your RedGuides 'Level 2' subscription.",
            )
            yield Button(
                "RedGuides 🍻",
                id="btn_redguides",
                variant="default",
                classes="web_link",
            )

    def on_mount(self) -> None:
        # observe app-owned state directly
        self.watch(self.app, "username", self._refresh_account_state)
        self.watch(self.app, "is_level_2", self._refresh_account_state)

    def _refresh_account_state(self) -> None:
        app = self.app
        self.query_one("#btn_ding", Button).display = app.is_level_2 is not True
        if not app.username:
            return  # keep the "Loading..." compose default until the level check resolves
        if app.is_level_2 is True:
            text = f"[italic][bold]{app.username}, thank you for being level 2[/bold][/italic] 💛"
        elif app.is_level_2 is False:
            text = f"Hey {app.username}, you're level 1 😞 some resources won't be downloaded."
        else:
            text = f"Hey {app.username}, we couldn't verify your account level."
        self.query_one("#account_label", Label).update(text)

    #
    # Event handlers for widgets on this tab
    #

    @on(Button.Pressed, "#btn_watched")
    def handle_btn_watched_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community/watched/resources")

    @on(Button.Pressed, "#btn_account")
    def handle_btn_account_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community/amember-sso/?to=member")

    @on(Button.Pressed, "#btn_licensed")
    def handle_btn_licensed_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link(
            "https://www.redguides.com/community/resources/market-place-user/licenses"
        )

    @on(Button.Pressed, "#btn_redguides")
    def handle_btn_redguides_pressed(self, event: Button.Pressed) -> None:
        self.app.action_link("https://www.redguides.com/community")

    @on(Button.Pressed, "#btn_ding")
    def handle_btn_ding_pressed(self, event: Button.Pressed) -> None:
        self.app.handle_ding_check()
