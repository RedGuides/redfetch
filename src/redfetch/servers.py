"""Manage emulator server profiles.

Other modules should use this API instead of accessing ``SERVERS.*`` directly.
"""
import os
import re

from redfetch import config


# Slugs must work as bare TOML keys and CLI arguments.
SERVER_SLUG_RE = re.compile(r"[a-z0-9_-]+")

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


def validate_server_slug(slug: str, *, must_be_new: bool = False) -> str:
    if not slug or not isinstance(slug, str):
        raise ValueError("Server name can't be empty.")
    if not SERVER_SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"Invalid server name '{slug}': use lowercase letters, digits, '-' or '_'."
        )
    if slug.upper() in config.ENV_TOKENS:
        raise ValueError(f"'{slug}' is a reserved name (client environment).")
    if must_be_new:
        for env in config.ENV_TOKENS:
            if slug in list_servers(env):
                raise ValueError(f"Server name '{slug}' is already in use ({env}).")
    return slug


def list_servers(env: str = "EMU") -> dict[str, dict]:
    """Return server profiles keyed by slug as plain dictionaries."""
    servers = config.settings.from_env(env).get("SERVERS") or {}
    if not isinstance(servers, dict):
        return {}
    return {
        str(slug): config._to_plain(table)
        for slug, table in servers.items()
        if isinstance(table, dict)
    }


def get_active_server(env: str = "EMU") -> str | None:
    slug = config.settings.from_env(env).get("ACTIVE_SERVER")
    return str(slug) if slug else None


def is_server_configured(slug: str, env: str = "EMU") -> bool:
    server = list_servers(env).get(slug)
    if not server:
        return False
    return bool(server.get("opt_in")) and bool(str(server.get("eqpath") or "").strip())


def is_known_server(slug: str, env: str = "EMU") -> bool:
    """True when the slug ships in the bundled settings.toml."""
    return slug in _bundle_servers(env)


def _bundle_servers(env: str = "EMU") -> dict[str, dict]:
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


def _save_reload_patch(path, doc, written):
    """Persist one batched write, then patch dynaconf's caches per key."""
    config.save_config(path, doc)
    config.settings.reload()

    # reload() leaves from_env() caches stale.
    emu_clone = config.settings.from_env("EMU")
    mirror_base = str(getattr(config.settings, "current_env", "")).upper() == "EMU"
    for key, value in written.items():
        emu_clone.set(key, value)
        if mirror_base:
            config.settings.set(key, value)


def switch_server(slug: str) -> None:
    """Switch the active emu server."""
    _require_init()

    slug = validate_server_slug(slug)
    incoming = list_servers("EMU").get(slug)
    if incoming is None:
        raise ServerSwitchError(f"Unknown server '{slug}'.")
    if not str(incoming.get("eqpath") or "").strip():
        raise ServerSwitchError(
            f"Server '{slug}' has no EverQuest folder yet — configure it first."
        )
    if not incoming.get("opt_in"):
        raise ServerSwitchError(f"Server '{slug}' isn't set up — configure it first.")

    outgoing = get_active_server("EMU")
    emu_view = config.settings.from_env("EMU")
    base_emu = config._base_settings().from_env("EMU")

    path, doc = _load_local()
    emu_table = config._descend_tables(doc, ("EMU",))

    written = {}

    # Don't recreate a deleted outgoing server.
    saved_back = None
    if outgoing and is_server_configured(outgoing):
        saved_back = {
            slot: _normalize_slot_value(slot, _read_env_slot(emu_view, slot))
            for slot in SERVER_SLOT_PATHS
        }
        for slot, value in saved_back.items():
            snap_path = ("SERVERS", outgoing) + _slot_snapshot_path(slot)
            config._descend_tables(emu_table, snap_path[:-1])[snap_path[-1]] = value
            written[".".join(snap_path)] = value
    elif outgoing:
        print(f"'{outgoing}' is no longer configured; its settings won't be saved back.")

    # Missing profile values inherit the bundled EMU defaults.
    for slot in SERVER_SLOT_PATHS:
        if outgoing == slug and saved_back is not None:
            value = saved_back[slot]
        else:
            value = _walk_get(incoming, _slot_snapshot_path(slot))
            if value is None:
                value = _read_env_slot(base_emu, slot)
            value = _normalize_slot_value(slot, value)
        config._descend_tables(emu_table, slot[:-1])[slot[-1]] = value
        written[".".join(slot)] = value

    # Avoid writing maps to <drive>:\maps.
    if not written["EQPATH"].strip() and any(
        written[f"SPECIAL_RESOURCES.{resource_id}.opt_in"]
        for resource_id in config.MAPS_MAP.values()
    ):
        raise ServerSwitchError(
            f"Server '{slug}' has no EverQuest folder but map downloads are enabled — "
            "set its EQ folder first."
        )

    emu_table["ACTIVE_SERVER"] = slug
    written["ACTIVE_SERVER"] = slug

    _save_reload_patch(path, doc, written)
    print(f"Active server: {slug}")


def add_server(slug: str, *, eqpath: str, label: str | None = None,
               patcher_url: str | None = None) -> None:
    """Configure a known server or create a custom one; the first becomes active."""
    _require_init()

    slug = validate_server_slug(slug)
    eqpath = str(eqpath or "").strip()
    if not eqpath:
        raise ValueError(f"Server '{slug}' needs an EverQuest folder.")

    known = slug in _bundle_servers()
    if not known and slug not in list_servers():
        validate_server_slug(slug, must_be_new=True)  # cross-client uniqueness
    first = not any(is_server_configured(s) for s in list_servers())

    path, doc = _load_local()
    written = {}
    snap = config._descend_tables(doc, ("EMU", "SERVERS", slug))
    if label and not known:  # the bundle owns known labels
        snap["label"] = label
        written[f"SERVERS.{slug}.label"] = label
    snap["opt_in"] = True
    snap["eqpath"] = eqpath
    written[f"SERVERS.{slug}.opt_in"] = True
    written[f"SERVERS.{slug}.eqpath"] = eqpath
    if patcher_url:
        snap["patcher_url"] = patcher_url
        written[f"SERVERS.{slug}.patcher_url"] = patcher_url

    if first:
        # Today's setup becomes server #1: seed from the env slots, no switch.
        emu_view = config.settings.from_env("EMU")
        for slot in SERVER_SLOT_PATHS:
            if slot == ("EQPATH",):
                continue  # the caller's folder wins
            value = _normalize_slot_value(slot, _read_env_slot(emu_view, slot))
            snap_path = _slot_snapshot_path(slot)
            config._descend_tables(snap, snap_path[:-1])[snap_path[-1]] = value
            written[f"SERVERS.{slug}." + ".".join(snap_path)] = value
        emu_table = config._descend_tables(doc, ("EMU",))
        emu_table["EQPATH"] = eqpath
        emu_table["ACTIVE_SERVER"] = slug
        written["EQPATH"] = eqpath
        written["ACTIVE_SERVER"] = slug

    _save_reload_patch(path, doc, written)
    print(f"Server '{slug}' added." + (" Now active." if first else ""))


def delete_server(slug: str) -> None:
    """Remove a custom server; a known one reverts to available."""
    _require_init()

    slug = validate_server_slug(slug)
    known = slug in _bundle_servers()
    if not known and slug not in list_servers():
        raise ValueError(f"Unknown server '{slug}'.")

    path, doc = _load_local()
    written = {}
    emu_table = doc.get("EMU")
    servers_table = emu_table.get("SERVERS") if emu_table is not None else None
    if servers_table is not None:
        servers_table.pop(slug, None)
    # Known slugs revert to their bundled entry; customs disappear.
    written[f"SERVERS.{slug}"] = _bundle_servers().get(slug) if known else None

    if get_active_server() == slug and emu_table is not None:
        emu_table.pop("ACTIVE_SERVER", None)
        written["ACTIVE_SERVER"] = None

    _save_reload_patch(path, doc, written)
    print(f"Server '{slug}' removed.")


def rename_server(old: str, new: str) -> None:
    """Re-key a custom server; known server names are fixed by the bundle."""
    _require_init()

    old = validate_server_slug(old)
    if old in _bundle_servers():
        raise ValueError(f"'{old}' is a known server; its name can't be changed.")
    if old not in list_servers():
        raise ValueError(f"Unknown server '{old}'.")
    new = validate_server_slug(new, must_be_new=True)

    path, doc = _load_local()
    emu_table = doc.get("EMU")
    servers_table = emu_table.get("SERVERS") if emu_table is not None else None
    if servers_table is None or old not in servers_table:
        raise ValueError(f"Server '{old}' has no local settings to rename.")

    written = {}
    servers_table[new] = servers_table.pop(old)
    written[f"SERVERS.{old}"] = None
    written[f"SERVERS.{new}"] = config._to_plain(servers_table[new])

    if get_active_server() == old:
        emu_table["ACTIVE_SERVER"] = new
        written["ACTIVE_SERVER"] = new

    _save_reload_patch(path, doc, written)
    print(f"Server '{old}' renamed to '{new}'.")
