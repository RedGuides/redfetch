"""Multi-server (emu) domain: active-server state, switching, and slug registry."""
# standard
import os
import re
from dataclasses import dataclass

# local
from redfetch import config
from redfetch import store


# Slugs must work as bare TOML keys and CLI arguments.
SERVER_SLUG_RE = re.compile(r"[a-z0-9_-]+")

# Where the bare setup parks its values while a named server holds the slots.
GENERIC_SNAPSHOT_KEY = "GENERIC"

# reserved so a user server can never shadow the generic "any emu server"
BARE_SETUP_TOKEN = "none"

SERVER_SLOT_PATHS = (
    ("EQPATH",),
    *(
        ("SPECIAL_RESOURCES", resource_id, leaf)
        for resource_id in config.MAPS_MAP.values()
        for leaf in ("opt_in", "custom_path")
    ),
)


class ServerSwitchError(ValueError):
    """Blocked server switch."""


@dataclass(frozen=True)
class ServerContext:
    """One client's active server, uniform across every client."""
    label: str
    eqpath: str
    patcher_url: str = ""
    patcher_exe: str = ""
    guide: str = ""


def is_multi_server(env: str) -> bool:
    """True when *env* can have switchable servers."""
    return env in config.MULTI_SERVER_ENVS


def env_for_slug(slug: str) -> str | None:
    """The env that owns *slug*, or None. Raises ValueError if ambiguous."""
    found = [env for env in config.MULTI_SERVER_ENVS if slug in list_servers(env)]
    if len(found) > 1:
        raise ValueError(
            f"Server name '{slug}' is ambiguous — it exists under "
            f"{' and '.join(config.ENVS[e] for e in found)}."
        )
    return found[0] if found else None


def validate_server_slug(slug: str, *, must_be_new: bool = False) -> str:
    if not slug or not isinstance(slug, str):
        raise ValueError("Server name can't be empty.")
    if not SERVER_SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"Invalid server name '{slug}': use lowercase letters, digits, '-' or '_'."
        )
    if slug.upper() in config.ENVS or slug == BARE_SETUP_TOKEN:
        raise ValueError(f"'{slug}' is a reserved name.")
    if must_be_new:
        for env in config.ENVS:
            if slug in list_servers(env):
                raise ValueError(
                    f"Server name '{slug}' is already in use on {config.ENVS[env]}."
                )
    return slug


def list_servers(env: str) -> dict[str, dict]:
    """Return the env's servers keyed by slug as plain dictionaries."""
    servers = config.settings.from_env(env).get("SERVERS") or {}
    if not isinstance(servers, dict):
        return {}
    return {
        str(slug): config._to_plain(table)
        for slug, table in servers.items()
        if isinstance(table, dict)
    }


def server_label(slug: str, env: str) -> str:
    """Display label for a server, falling back to its slug."""
    return list_servers(env).get(slug, {}).get("label") or slug


def get_active_server(env: str) -> str | None:
    """The active server slug, or None."""

    slug = config.settings.from_env(env).get("ACTIVE_SERVER")
    return str(slug) if slug else None


def active_server_slug(env: str) -> str:
    """The slug that keys per-server db rows."""
    if not is_multi_server(env):
        return ""
    return get_active_server(env) or env.lower()


def is_server_configured(slug: str, env: str) -> bool:
    server = list_servers(env).get(slug)
    if not server:
        return False
    return bool(server.get("opt_in")) and bool(str(server.get("eqpath") or "").strip())


def is_known_server(slug: str, env: str) -> bool:
    """True when the slug ships in the bundled settings.toml."""
    return slug in _bundle_servers(env)


def active_server_context(env: str) -> ServerContext:
    """The active server's env-slot values plus its SERVERS extras (patcher, guide)."""
    slug = get_active_server(env) if is_multi_server(env) else None
    # A hand-edited ACTIVE_SERVER can name an entry that isn't there; no extras is the safe read.
    entry = (list_servers(env).get(slug) or {}) if slug else {}
    return ServerContext(
        label=str(entry.get("label") or slug or _implicit_server_label(env)),
        eqpath=str(config.settings.from_env(env).get("EQPATH") or ""),
        patcher_url=str(entry.get("patcher_url") or ""),
        patcher_exe=str(entry.get("patcher_exe") or ""),
        guide=str(entry.get("guide") or ""),
    )


def server_context(slug: str, env: str, *, eqpath: str) -> ServerContext:
    """A server's patcher, login, and label at an arbitrary folder. Before the folder exists."""
    entry = list_servers(env).get(slug)
    if entry is None:
        raise ValueError(f"Unknown server '{slug}' on {config.ENVS[env]}.")
    return ServerContext(
        label=str(entry.get("label") or slug),
        eqpath=str(eqpath or ""),
        patcher_url=str(entry.get("patcher_url") or ""),
        patcher_exe=str(entry.get("patcher_exe") or ""),
        guide=str(entry.get("guide") or ""),
    )


def _implicit_server_label(env: str) -> str:
    """What to call a server that has no entry to name it."""
    return config.BARE_SERVER_LABEL if is_multi_server(env) else config.ENVS[env]


def _bundle_servers(env: str) -> dict[str, dict]:
    """Known servers shipped in the bundled settings.toml."""
    servers = config._base_settings().from_env(env).get("SERVERS") or {}
    if not isinstance(servers, dict):
        return {}
    return {
        str(slug): config._to_plain(table)
        for slug, table in servers.items()
        if isinstance(table, dict)
    }


def _slot_snapshot_path(slot):
    return ("eqpath",) if slot == ("EQPATH",) else slot


def _normalize_slot_value(slot, value):
    return bool(value) if slot[-1] == "opt_in" else str(value or "")


def _read_env_slot(env_settings, slot):
    current = env_settings.get(slot[0])
    for key in slot[1:]:
        if current is None or not hasattr(current, "get"):
            return None
        current = current.get(key)
    return current


def _snapshot_slot_values(snapshot, base_view) -> dict:
    """A snapshot's slot values; missing ones inherit the bundled env defaults."""
    values = {}
    for slot in SERVER_SLOT_PATHS:
        value = _walk_get(snapshot, _slot_snapshot_path(slot))
        if value is None:  # absent, not falsey
            value = _read_env_slot(base_view, slot)
        values[slot] = _normalize_slot_value(slot, value)
    return values


def _write_env_slots(env_table, values) -> None:
    for slot, value in values.items():
        config._descend_tables(env_table, slot[:-1])[slot[-1]] = value


def _walk_get(mapping, keys):
    """Read a nested mapping while ignoring key case."""
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        if key in current:
            current = current[key]
            continue
        lowered = key.lower()
        for candidate, value in current.items():
            if isinstance(candidate, str) and candidate.lower() == lowered:
                current = value
                break
        else:
            return None
    return current


def _require_init():
    if config.settings is None or config.config_dir is None:
        raise RuntimeError("Configuration has not been initialized.")


def _load_local():
    path = os.path.join(config.config_dir, "settings.local.toml")
    config.ensure_config_file_exists(path)
    return path, config.load_config(path)


def _save_and_reload(path, doc):
    """Persist one batched write, then refresh dynaconf from the file."""
    config.save_config(path, doc)
    config.reload_settings()


def _snapshot_path(slug: str | None) -> tuple:
    """Where a server's dormant values live: named servers nest, the bare setup doesn't."""
    return ("SERVERS", slug) if slug else (GENERIC_SNAPSHOT_KEY,)


def _maps_enabled(applied: dict) -> bool:
    return any(
        applied[("SPECIAL_RESOURCES", resource_id, "opt_in")]
        for resource_id in config.MAPS_MAP.values()
    )


def _clamp_maps_without_folder(values: dict) -> bool:
    """Turn maps off when there's no folder; True when that changed something."""
    if values[("EQPATH",)].strip() or not _maps_enabled(values):
        return False
    for resource_id in config.MAPS_MAP.values():
        values[("SPECIAL_RESOURCES", resource_id, "opt_in")] = False
    return True


def _apply_switch(env: str, incoming_slug: str | None, incoming: dict) -> tuple[list, dict, str, dict]:
    """Save the outgoing server's slots away and stage the incoming ones."""
    outgoing = get_active_server(env)
    env_view = config.settings.from_env(env)
    base_view = config._base_settings().from_env(env)
    notices = []

    path, doc = _load_local()
    env_table = config._descend_tables(doc, (env,))

    # A named outgoing server that's been deleted must not be recreated
    saved_back = None
    if outgoing is None or is_server_configured(outgoing, env):
        saved_back = {
            slot: _normalize_slot_value(slot, _read_env_slot(env_view, slot))
            for slot in SERVER_SLOT_PATHS
        }
        snapshot_root = _snapshot_path(outgoing)
        for slot, value in saved_back.items():
            snap_path = snapshot_root + _slot_snapshot_path(slot)
            config._descend_tables(env_table, snap_path[:-1])[snap_path[-1]] = value
    else:
        notices.append(
            f"'{outgoing}' is no longer configured. Its settings won't be saved back."
        )

    # Re-switching to the active server
    if outgoing == incoming_slug and saved_back is not None:
        applied = saved_back
    else:
        applied = _snapshot_slot_values(incoming, base_view)
    _write_env_slots(env_table, applied)
    return notices, doc, path, applied


def switch_server(slug: str) -> list[str]:
    """Switch to a named server (the env is derived from the slug)."""
    _require_init()

    slug = validate_server_slug(slug)
    env = env_for_slug(slug)  # raises on an ambiguous slug
    if env is None:
        raise ServerSwitchError(f"Unknown server '{slug}'.")
    incoming = list_servers(env)[slug]
    if not str(incoming.get("eqpath") or "").strip():
        raise ServerSwitchError(
            f"Server '{slug}' has no EverQuest folder yet — configure it first."
        )
    if not incoming.get("opt_in"):
        raise ServerSwitchError(f"Server '{slug}' isn't set up — configure it first.")

    notices, doc, path, applied = _apply_switch(env, slug, incoming)

    # Avoid writing maps to <drive>:\maps.
    if not applied[("EQPATH",)].strip() and _maps_enabled(applied):
        raise ServerSwitchError(
            f"Server '{slug}' has no EverQuest folder but map downloads are enabled — "
            "set its EQ folder first."
        )

    config._descend_tables(doc, (env,))["ACTIVE_SERVER"] = slug

    _save_and_reload(path, doc)
    return notices


def switch_to_generic(env: str) -> list[str]:
    """Switch to the bare any server setup"""
    _require_init()
    if not is_multi_server(env):
        raise ServerSwitchError(f"'{env}' doesn't have switchable servers.")

    notices, doc, path, applied = _apply_switch(env, None, _generic_snapshot(env))
    env_table = config._descend_tables(doc, (env,))
    if _clamp_maps_without_folder(applied):
        _write_env_slots(env_table, applied)
        notices.append("Map downloads are off until you set an EverQuest folder.")

    env_table.pop("ACTIVE_SERVER", None)

    _save_and_reload(path, doc)
    return notices


def _generic_snapshot(env: str) -> dict:
    """The bare setup's parked values, or {} before it has ever been parked."""
    snapshot = config.settings.from_env(env).get(GENERIC_SNAPSHOT_KEY)
    return config._to_plain(snapshot) if isinstance(snapshot, dict) else {}


def generic_eqpath(env: str) -> str:
    """The folder the bare setup would restore"""
    return str(_generic_snapshot(env).get("eqpath") or "")


def add_server(slug: str, *, env: str, eqpath: str, label: str | None = None,
               patcher_url: str | None = None, patcher_exe: str | None = None) -> None:
    """Configure a known server or create a custom one."""
    _require_init()

    slug = validate_server_slug(slug)
    if not is_multi_server(env):
        raise ValueError(f"'{env}' doesn't have switchable servers.")
    eqpath = str(eqpath or "").strip()
    if not eqpath:
        raise ValueError(f"Server '{slug}' needs an EverQuest folder.")

    known = slug in _bundle_servers(env)
    if not known and slug not in list_servers(env):
        validate_server_slug(slug, must_be_new=True)  # cross-client uniqueness

    path, doc = _load_local()
    snap = config._descend_tables(doc, (env, "SERVERS", slug))
    if label and not known:  # the bundle owns known labels
        snap["label"] = label
    snap["opt_in"] = True
    snap["eqpath"] = eqpath
    if patcher_url:
        snap["patcher_url"] = patcher_url
    if patcher_exe:
        snap["patcher_exe"] = patcher_exe

    if slug == get_active_server(env):
        # The env slots hold the active server's values
        config._descend_tables(doc, (env,))["EQPATH"] = eqpath

    _save_and_reload(path, doc)


def delete_server(slug: str, *, env: str) -> None:
    """Remove a custom server; a known one reverts to available."""
    _require_init()

    slug = validate_server_slug(slug)
    known = slug in _bundle_servers(env)
    if not known and slug not in list_servers(env):
        raise ValueError(f"Unknown server '{slug}'.")

    was_active = get_active_server(env) == slug
    path, doc = _load_local()
    env_table = doc.get(env)
    servers_table = env_table.get("SERVERS") if env_table is not None else None
    if servers_table is not None:
        # Known slugs revert to their bundled entry; customs disappear.
        servers_table.pop(slug, None)

    if was_active:
        # Remove the deleted server's values
        base_view = config._base_settings().from_env(env)
        env_table = config._descend_tables(doc, (env,))
        applied = _snapshot_slot_values(_generic_snapshot(env), base_view)
        _clamp_maps_without_folder(applied)
        _write_env_slots(env_table, applied)
        env_table.pop("ACTIVE_SERVER", None)

    _save_and_reload(path, doc)
    store.purge_server_rows(store.db_name(env), slug)


def rename_server(old: str, new: str, *, env: str) -> None:
    """Re-key a custom server; known server names are fixed by the bundle."""
    _require_init()

    old = validate_server_slug(old)
    if old in _bundle_servers(env):
        raise ValueError(f"'{old}' is a known server; its name can't be changed.")
    if old not in list_servers(env):
        raise ValueError(f"Unknown server '{old}'.")
    new = validate_server_slug(new, must_be_new=True)

    path, doc = _load_local()
    env_table = doc.get(env)
    servers_table = env_table.get("SERVERS") if env_table is not None else None
    if servers_table is None or old not in servers_table:
        raise ValueError(f"Server '{old}' has no local settings to rename.")

    servers_table[new] = servers_table.pop(old)

    if get_active_server(env) == old:
        env_table["ACTIVE_SERVER"] = new

    _save_and_reload(path, doc)
    store.rekey_server_rows(store.db_name(env), old, new)
