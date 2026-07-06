# standard imports
import sys
import os
from enum import Enum
from pathlib import Path
from typing import Optional
import asyncio

# third-party imports
from rich.prompt import Confirm, Prompt
from rich.console import Console
import typer

# local imports
from redfetch import api
from redfetch import auth
from redfetch import config
from redfetch import meta
from redfetch import net
from redfetch import post_update
from redfetch import processes
from redfetch import utils
from redfetch import push
from redfetch import sync
from redfetch import store
from redfetch import shortcuts
from redfetch.runtime_errors import exit_with_fatal_error


app = typer.Typer(
    help="[bold red]redfetch[/bold red] - RedGuides resource management tool. Run without arguments to launch the [italic]Terminal User Interface[/italic].",
    rich_markup_mode="rich"
)

console = Console()


class Env(str, Enum):
    LIVE = "LIVE"
    TEST = "TEST"
    EMU = "EMU"


def parse_resource_id_or_fail(value: str) -> str:
    """Accept either an integer ID or a URL that includes a recognizable ID."""
    value_stripped = value.strip()
    if value_stripped.isdigit():
        return value_stripped
    parsed = utils.parse_resource_id(value_stripped)
    if parsed is None:
        raise typer.BadParameter("Provide a resource ID or a recognized RedGuides URL.")
    return parsed


def _apply_server_override(server: "Optional[Env]" = None) -> None:
    if server is not None and server.value != config.settings.ENV:
        config.select_environment_in_memory(server.value)


def _initialize_auth():
    """Initialize configuration, update check, and auth (no DB, no network)."""
    config.initialize_config()
    if os.environ.get('CI') != 'true':
        _ = meta.check_for_update()
    auth.initialize_keyring()
    auth.authorize()


def initialize_db_only(server: "Optional[Env]" = None):
    """Initialize configuration, auth, and local cache database (no network)."""
    _initialize_auth()
    _apply_server_override(server)
    db_name = f"{config.settings.ENV}_resources.db"
    store.initialize_db(db_name)
    db_path = store.get_db_path(db_name)
    return db_name, db_path


# ===== CLI prompt helpers =====

def prompt_navmesh_opt_in() -> bool | None:
    """Prompt user about navmesh downloads if not configured."""
    from redfetch import navmesh

    # Check if VVMQ path exists (navmesh requires it)
    vvmq_path = utils.get_vvmq_path()
    if not vvmq_path:
        return None  # Can't do navmesh without VVMQ

    opt_in = navmesh.get_navmesh_opt_in()

    if opt_in is not None:
        return None  # Already configured, use existing setting

    # Prompt user
    user_choice = Prompt.ask(
        "🧭 Download navigation meshes? (will overwrite, protect your custom meshes in settings.local.toml)",
        choices=["yes", "no", "always", "never"],
        default="yes",
    )

    if user_choice == "yes":
        return True  # One-time yes
    elif user_choice == "always":
        config.update_setting(["NAVMESH_OPT_IN"], True)
        console.print("Updated settings to always download navmeshes.")
        return None  # Config is now set, use it
    elif user_choice == "never":
        config.update_setting(["NAVMESH_OPT_IN"], False)
        console.print("Updated settings to never download navmeshes.")
        return None  # Config is now set, use it
    else:  # "no"
        return False  # One-time no


class _CliPostUpdate:
    """CLI adapter for post_update.execute: rich prompts, console output."""

    def notify(self, message: str, *, error: bool = False) -> None:
        console.print(message)

    async def confirm_restart(self) -> bool:
        try:
            choice = Prompt.ask(
                "Restart MacroQuest to apply the update now?",
                choices=["yes", "no"],
                default="yes",
            )
        except (KeyboardInterrupt, EOFError):
            return False  # headless/no-stdin: skip the restart, update applies next launch
        return choice == "yes"

    async def ask_cold_start(self) -> post_update.ColdStartChoice:
        try:
            return Prompt.ask(
                "Do you want to start MacroQuest now?",
                choices=["yes", "no", "always", "never"],
                default="yes",
            )
        except (KeyboardInterrupt, EOFError):
            return "no"  # headless/no-stdin: don't start MacroQuest

    def auto_run_persisted(self, value: bool) -> None:
        pass  # the policy notifies; nothing to sync in the CLI

    async def wait_for_eq_close(self) -> bool:
        while processes.get_eqgame_process_pids():
            try:
                Prompt.ask(
                "[yellow]EverQuest is still running.[/yellow] Close it, then press "
                "[bold]Enter[/bold] to restart MacroQuest, or [bold]Ctrl-C[/bold] to "
                "skip (update applies next MacroQuest launch)"
            )
            except (KeyboardInterrupt, EOFError):
                return False
        return True


async def handle_download_watched_async(db_path: str, headers: dict) -> bool:
    """Run the main 'update watched' flow using async network calls."""
    if await net.is_mq_down():
        console.print(
            "[bold yellow]Warning:[/bold yellow] [blink bold red]MQ appears to be down[/blink bold red] for a patch, so it's not likely to work."
        )
        continue_download = Confirm.ask(
            "Do you want to continue with the download?", default=False
        )
        if not continue_download:
            console.print("Download cancelled by user.")
            return False

    navmesh_override = prompt_navmesh_opt_in()

    outcome = await sync.run_sync(
        db_path, headers,
        navmesh_override=navmesh_override,
    )
    # offer regardless of overall success:
    await post_update.offer(outcome, _CliPostUpdate())
    return outcome.success


async def update_command_async(db_name: str, db_path: str, force: bool) -> None:
    await asyncio.to_thread(utils.sweep_stale_update_debris)
    headers = await auth.get_api_headers()
    if not (await api.get_sync_info(headers)).is_level_2:
        console.print(
            "[bold yellow]Warning:[/bold yellow] You're not level 2 on RedGuides, so some resources will not be downloadable."
        )
    if force:
        with store.get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            console.print(
                "Force download requested. All watched resources will be re-downloaded."
            )
            store.reset_download_dates(cursor)
    await handle_download_watched_async(db_path, headers)


async def download_command_async(db_name: str, db_path: str, id_or_url: str, force: bool) -> None:
    headers = await auth.get_api_headers()
    rid = parse_resource_id_or_fail(id_or_url)
    if force:
        with store.get_db_connection(db_name) as conn:
            cursor = conn.cursor()
            store.reset_versions_for_resource(cursor, rid)
    console.print(f"Downloading resource {rid}.")
    await sync.run_sync(db_path, headers, [rid])


@app.command(
    "update",
    help="Update all [italic]watched[/italic] and special resources.",
    rich_help_panel="📦 Resource Management"
)
def update_command(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-download of all watched resources."),
    server: Optional[Env] = typer.Option(None, "--server", "-s", case_sensitive=False, help="Update this server for this run only, without changing your current server ([green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan])."),
):
    db_name, db_path = initialize_db_only(server=server)
    asyncio.run(update_command_async(db_name=db_name, db_path=db_path, force=force))


@app.command(
    "download",
    help="Download a specific resource by ID or URL.",
    rich_help_panel="📦 Resource Management"
)
def download(
    id_or_url: str = typer.Argument(..., metavar="ID_OR_URL", help="RedGuides resource ID or URL"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-download by resetting this resource's download date."),
    server: Optional[Env] = typer.Option(None, "--server", "-s", case_sensitive=False, help="Download from this server for this run only, without changing your current server ([green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan])."),
):
    db_name, db_path = initialize_db_only(server=server)
    asyncio.run(download_command_async(db_name=db_name, db_path=db_path, id_or_url=id_or_url, force=force))


def _has_auth_credentials() -> bool:
    """Peek at env / keyring for stored credentials (no network, no init)."""
    if os.environ.get("REDGUIDES_API_KEY"):
        return True
    try:
        import keyring
        token = keyring.get_password(auth.KEYRING_SERVICE_NAME, "access_token")
        return token is not None
    except Exception:
        return False


@app.command(
    "check",
    help=(
        "Non-interactive update check (for automation.)"
    ),
    rich_help_panel="📦 Resource Management",
)
def check_command(
    server: Optional[Env] = typer.Option(
        None, "--server", "-s", case_sensitive=False,
        help="Check this server's env for this run only, without persisting it ([green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan]).",
    ),
):
    from redfetch.config_firstrun import is_configured
    from redfetch import update_status

    requested_env = server.value if server else None

    # Pre-flight: not configured at all -> no init possible, but we can still record the verdict.
    if not is_configured():
        update_status.write_update_status(
            env=requested_env or Env.LIVE.value,
            auth_state="not_configured",
        )
        raise typer.Exit(0)

    try:
        config.initialize_config()
        # Honor --server for this run only; never persist (a "check" must not change the user's env).
        if requested_env:
            config.select_environment_in_memory(requested_env)
        env = config.settings.ENV
        auth.initialize_keyring()

        # MQ matches this against its own root to ignore stray copies.
        managed_path = utils.get_vvmq_path()

        if not _has_auth_credentials():
            update_status.write_update_status(env=env, auth_state="needs_login", managed_path=managed_path)
            raise typer.Exit(0)

        db_name = f"{env}_resources.db"
        store.initialize_db(db_name)
        db_path = store.get_db_path(db_name)

        auth_state, items = asyncio.run(_check_command_async(db_path))
        update_status.write_update_status(env=env, auth_state=auth_state, items=items, managed_path=managed_path)
        raise typer.Exit(0)

    except typer.Exit:
        raise
    except Exception:
        # Transient failure, no touch to update_status.json so we know checked_at didn't advance this cycle.
        raise typer.Exit(1)


async def _check_command_async(db_path: str) -> tuple[str, list[dict] | None]:
    from redfetch import update_status

    try:
        headers = await auth.get_api_headers()
    except RuntimeError:
        # Silent refresh failed / expired / logged out -> a gentle re-login nudge
        return "needs_login", None

    prepared = await sync.prepare_sync(db_path, headers)
    return "ok", update_status.build_items_from_plan(prepared.execution_plan)


@app.command(
    "ui",
    help="Launch the [italic]Terminal User Interface[/italic].",
    rich_help_panel="🔧 System & Utilities"
)
def run_tui():
    """Initialize configuration and launch the Terminal User Interface."""
    _initialize_auth()
    utils.sweep_stale_update_debris()
    from redfetch.terminal_ui import run_textual_ui
    run_textual_ui()


def _print_shortcut_table(entries, available) -> None:
    """List shortcuts (key, aliases, availability) for the bare `run`/`open` verb."""
    from rich.table import Table

    # ASCII-only content for legacy Windows consoles
    table = Table(show_header=True, header_style="bold")
    table.add_column("shortcut")
    table.add_column("aliases", style="dim")
    table.add_column("available", justify="center")
    table.add_column("description", style="dim")
    for entry in entries:
        mark = "[green]yes[/green]" if available(entry) else "[dim]no[/dim]"
        table.add_row(entry.key, ", ".join(entry.aliases) or "-", mark, entry.tooltip)
    console.print(table)


@app.command(
    "run",
    help="Run a shortcut (e.g. [bold]vvmq[/bold], [bold]eqbcs[/bold], [bold]myseq[/bold]). [bold]run[/bold] by itself will show a full list.",
    rich_help_panel="🔧 System & Utilities",
)
def run_shortcut_command(
    target: Optional[str] = typer.Argument(None, metavar="SHORTCUT", help="Shortcut to run: vvmq, eqbcs, eq, eqgame, etc."),
    server: Optional[Env] = typer.Option(None, "--server", "-s", case_sensitive=False, help="Run for this server this run only, without changing your current server ([green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan])."),
):
    config.initialize_config()
    _apply_server_override(server)
    if target is None:
        _print_shortcut_table(shortcuts.RUNNABLES, shortcuts.runnable_available)
        return
    runnable = shortcuts.find_runnable(target)
    if runnable is None:
        valid = ", ".join(r.key for r in shortcuts.RUNNABLES)
        raise typer.BadParameter(f"Unknown shortcut '{target}'. Try: {valid}")
    try:
        shortcuts.run(runnable)
        console.print(f"Started [bold]{runnable.executable}[/bold].")
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(f"[red]Couldn't run {runnable.key}:[/red] {exc}")
        raise typer.Exit(1)


@app.command(
    "open",
    help="Open a folder or file (e.g. [bold]downloads[/bold], [bold]eqhost[/bold]). [bold]open[/bold] by itself will show a full list.",
    rich_help_panel="🔧 System & Utilities",
)
def open_shortcut_command(
    target: Optional[str] = typer.Argument(None, metavar="SHORTCUT", help="Folder/file to open: downloads, vvmq, eq, etc."),
    server: Optional[Env] = typer.Option(None, "--server", "-s", case_sensitive=False, help="Resolve paths for this server this run only, without changing your current server ([green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan])."),
):
    config.initialize_config()
    _apply_server_override(server)
    if target is None:
        _print_shortcut_table(shortcuts.OPENABLES, shortcuts.openable_available)
        return
    openable = shortcuts.find_openable(target)
    if openable is None:
        valid = ", ".join(o.key for o in shortcuts.OPENABLES)
        raise typer.BadParameter(f"Unknown shortcut '{target}'. Try: {valid}")
    try:
        detail = shortcuts.open_target(openable)
        console.print(f"Opened [bold]{openable.key}[/bold]{(' ' + detail) if detail else ''}.")
    except (ValueError, OSError) as exc:
        console.print(f"[red]Couldn't open {openable.key}:[/red] {exc}")
        raise typer.Exit(1)


@app.command(
    "web",
    help="Launch the [bold]RedGuides.com[/bold] web interface.",
    rich_help_panel="🔧 System & Utilities"
)
def web_command():
    db_name, _db_path = initialize_db_only()
    try:
        asyncio.run(web_command_async(db_name=db_name))
    except KeyboardInterrupt:
        console.print("\nServer stopped by user (Ctrl+C).")


async def web_command_async(db_name: str) -> None:
    headers = await auth.get_api_headers()
    from .listener import run_server_async
    await run_server_async(
        config.settings, db_name, headers, config.CATEGORY_MAP
    )


@app.command(
    "list",
    help="List resources and dependencies in your local cache.",
    rich_help_panel="📦 Resource Management"
)
def resources_list_command():
    db_name, _db_path = initialize_db_only()
    with store.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        resources = store.list_resources(cursor)
        console.print("Resources:")
        for resource_id, title in resources:
            console.print(f"ID: {resource_id}, Title: {title}")
        dependencies = store.list_dependencies(cursor)
        console.print("Dependencies:")
        for resource_id, title in dependencies:
            console.print(f"ID: {resource_id}, Title: {title}")


@app.command(
    "reset",
    help="Reset download dates for [italic]watched resources[/italic] in the database.",
    rich_help_panel="📦 Resource Management"
)
def resources_reset_command():
    db_name, _db_path = initialize_db_only()
    with store.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        store.reset_download_dates(cursor)
    console.print("Reset download dates for watched resources.")


@app.command(
    "config",
    help="Update a setting by path and value.",
    rich_help_panel="🍔 Configuration"
)
def config_command(
    path: str = typer.Argument(..., metavar="SETTING_PATH", help="Dot-separated setting path (e.g., SPECIAL_RESOURCES.1974.opt_in)"),
    value: str = typer.Argument(..., metavar="VALUE", help="New value for the setting"),
    server: Optional[Env] = typer.Option(None, "--server", "-s", case_sensitive=False, help="Server to apply the change in ([green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan])"),
):
    config.initialize_config()
    setting_path_list = path.split('.')
    config.update_setting(setting_path_list, value, server.value if server else None)
    settings_env = server.value if server else config.settings.ENV
    db_name = f"{settings_env}_resources.db"
    store.initialize_db(db_name)
    console.print(f"Updated setting {path} to {value}{' for server ' + server.value if server else ''}.")


@app.command(
    "server",
    help="Switch the current server/environment to [green]LIVE[/green], [yellow]TEST[/yellow], or [cyan]EMU[/cyan].",
    rich_help_panel="🍔 Configuration"
)
def server_command(
    env: Env = typer.Argument(..., metavar="SERVER", case_sensitive=False, help="Server to use: [green]LIVE[/green], [yellow]TEST[/yellow], [cyan]EMU[/cyan]"),
):
    config.initialize_config()
    config.switch_environment(env.value)
    console.print(f"Environment updated to {env.value}.")
    console.print("New complete configuration:")
    typer.echo(config.settings.from_env(env.value).as_dict())


@app.command("show", hidden=True)
@app.command(
    "status",
    help="Show the configuration for the current or specified server.",
    rich_help_panel="🍔 Configuration"
)
def config_show_command(server: Optional[Env] = typer.Option(None, "--server", "-s", case_sensitive=False, help="Server to show (defaults to current)")):
    from rich.panel import Panel

    config.initialize_config()
    current_env = server.value if server else getattr(config.settings, "ENV", "UNKNOWN")
    env_settings = config.settings.from_env(current_env)

    # Core paths for this environment
    download_folder = env_settings.get("DOWNLOAD_FOLDER") or ""
    eq_path = env_settings.get("EQPATH") or ""

    panel_lines: list[str] = []

    # Only show top-level paths that are actually set
    if download_folder:
        panel_lines.append(f"[bold yellow]DOWNLOAD_FOLDER:[/bold yellow] {download_folder}")
    if eq_path:
        panel_lines.append(f"[bold yellow]EQPATH:[/bold yellow] {eq_path}")

    # Opted-in special resources with resolved paths, keyed by resource ID
    special_resources = env_settings.get("SPECIAL_RESOURCES", {})

    for resource_id, resource_info in special_resources.items():
        # Only show if opted in
        if not resource_info.get("opt_in"):
            continue

        # Get the resolved path
        custom_path = resource_info.get("custom_path", "")
        default_path = resource_info.get("default_path", "")

        if custom_path:
            resource_path = custom_path
        elif default_path:
            # If default_path is absolute, use it as-is
            if download_folder and not os.path.isabs(default_path):
                resource_path = os.path.join(download_folder, default_path)
            else:
                resource_path = default_path
        else:
            continue

        # Label only by resource ID
        panel_lines.append(f"[bold yellow]Resource {resource_id}:[/bold yellow] {resource_path}")

    # If nothing to show, still render an empty-but-clear panel
    if not panel_lines:
        panel_lines.append("[dim]No paths are currently configured or opted in for this environment.[/dim]")

    console.print(Panel("\n".join(panel_lines), expand=False))

    # Optional: Show full settings dict with a label
    console.print("\n[dim][italic]Full configuration (for debugging):[/italic][/dim]")
    typer.echo(env_settings.as_dict())


@app.command("push", hidden=True, rich_help_panel="📤 Publishing")
@app.command(
    "publish",
    help="Publish updates to a [bold]RedGuides[/bold] resource.",
    rich_help_panel="📤 Publishing"
)
def publish_command(
    ctx: typer.Context,
    resource_id: int = typer.Argument(..., help="Existing RedGuides resource ID"),
    description: Optional[Path] = typer.Option(None, "--description", "-d", metavar="README.md", help="Path to a description file (e.g. README.md) to become the overview description.", exists=True, file_okay=True, dir_okay=False, readable=True, resolve_path=True),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="New version string (e.g., v1.0.1)"),
    message: Optional[Path] = typer.Option(None, "--message", "-m", metavar="CHANGELOG.md | MESSAGE", help="Path to [italic]CHANGELOG.md[/italic] (keep a changelog), other message file, or a direct message string.", exists=False),
    file: Optional[Path] = typer.Option(None, "--file", "-f", metavar="FILE.zip", help="Path to your zipped release file", exists=True, file_okay=True, dir_okay=False, readable=True, resolve_path=True),
    domain: Optional[str] = typer.Option(None, "--domain", help="If description or message is a .md file with relative URLs, resolve them to this domain (e.g., https://raw.githubusercontent.com/your/repo/main/)")
):
    # 'push' is the deprecated alias for 'publish' (identical params); warn only under that name.
    if ctx.info_name == "push":
        console.print("[yellow]Warning:[/yellow] 'push' is deprecated. Use 'redfetch publish' instead.")
    from types import SimpleNamespace
    args = SimpleNamespace(
        resource_id=resource_id,
        description=str(description) if isinstance(description, Path) else description,
        version=version,
        message=str(message) if isinstance(message, Path) else message,
        file=str(file) if isinstance(file, Path) else file,
        domain=domain,
    )
    push.handle_cli(args)


@app.command(
    "version",
    help="Show version and exit.",
    rich_help_panel="🔧 System & Utilities"
)
def version_command():
    console.print(f"redfetch {meta.get_current_version()}")


@app.command(
    "uninstall",
    help="Uninstall [bold]redfetch[/bold] and clean up data.",
    rich_help_panel="🔧 System & Utilities"
)
def uninstall_command():
    meta.uninstall()


@app.command(
    "logout",
    help="Log out and clear cached token and API cache.",
    rich_help_panel="🔧 System & Utilities"
)
def auth_logout():
    config.initialize_config()
    API_KEY = os.environ.get('REDGUIDES_API_KEY')
    if not API_KEY:
        auth.initialize_keyring()
        auth.logout()
        console.print("Logged out successfully.")
    else:
        console.print("Cannot logout when using API key from environment variable.")


# ============================================================================
# LEGACY/DEPRECATED COMMAND ALIASES
# ============================================================================


def legacy_callback_factory(new_command: str, invoke_func=None, **invoke_kwargs):
    """Factory to create deprecation callbacks that forward to new commands."""
    def callback(ctx: typer.Context, value):
        if ctx.resilient_parsing or not value:
            return value
        console.print(f"[bold yellow blink]Warning:[/bold yellow blink] This flag is deprecated! Use 'redfetch {new_command}' instead.")
        if invoke_func:
            ctx.invoke(invoke_func, **invoke_kwargs)
        raise typer.Exit()
    return callback


def legacy_switch_env_callback(ctx: typer.Context, value: Optional[Env]):
    """Deprecated --switch-env handler that forwards to the 'server' subcommand."""
    if ctx.resilient_parsing or value is None:
        return value
    console.print("[yellow]Warning:[/yellow] --switch-env is deprecated. Use 'redfetch server ENV' instead.")
    ctx.invoke(server_command, env=value)
    raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    # Legacy: --switch-env ENV
    switch_env: Optional[Env] = typer.Option(
        None, "--switch-env", is_eager=True, case_sensitive=False, hidden=True,
        callback=legacy_switch_env_callback,
        metavar="ENV", help="(Deprecated) Use 'server' subcommand instead.",
    ),
    # Legacy: --download-watched
    download_watched: bool = typer.Option(
        False, "--download-watched", is_eager=True, hidden=True,
        callback=legacy_callback_factory("update", update_command, force=False),
        help="(Deprecated) Use 'update' subcommand instead.",
    ),
    # Legacy: --force-download
    force_download: bool = typer.Option(
        False, "--force-download", is_eager=True, hidden=True,
        callback=legacy_callback_factory("update --force", update_command, force=True),
        help="(Deprecated) Use 'update --force' instead.",
    ),
    # Legacy: --serve
    serve: bool = typer.Option(
        False, "--serve", is_eager=True, hidden=True,
        callback=legacy_callback_factory("web", web_command),
        help="(Deprecated) Use 'web' subcommand instead.",
    ),
    # Legacy: --version
    show_version: bool = typer.Option(
        False, "--version", is_eager=True, hidden=True,
        callback=legacy_callback_factory("version", version_command),
        help="(Deprecated) Use 'version' subcommand instead.",
    ),
    # Legacy: --logout
    do_logout: bool = typer.Option(
        False, "--logout", is_eager=True, hidden=True,
        callback=legacy_callback_factory("logout", auth_logout),
        help="(Deprecated) Use 'logout' subcommand instead.",
    ),
    # Legacy: --uninstall
    do_uninstall: bool = typer.Option(
        False, "--uninstall", is_eager=True, hidden=True,
        callback=legacy_callback_factory("uninstall", uninstall_command),
        help="(Deprecated) Use 'uninstall' subcommand instead.",
    ),
):
    """redfetch - RedGuides resource management tool."""
    pass


# ============================================================================
# END LEGACY/DEPRECATED COMMANDS
# ============================================================================

def main():
    try:
        # Launch TUI when no arguments are provided
        if len(sys.argv) == 1:
            run_tui()
            return
        # `redfetch help` → show top-level help 
        if len(sys.argv) == 2 and sys.argv[1] == "help":
            sys.argv[1] = "--help"
        app()
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        exit_with_fatal_error(exc)


if __name__ == "__main__":
    main()
