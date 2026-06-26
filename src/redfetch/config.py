# standard
import json
import os
import platform
import re
import shutil

# third-party
import tomlkit
from dynaconf import Dynaconf, Validator, ValidationError
from platformdirs import user_config_dir, user_data_dir

# Parent Category to folder
CATEGORY_MAP = {
    8: "macros",
    11: "plugins",
    25: "lua"
}

# Resource to MQ version
VANILLA_MAP = {
    1974: "LIVE",
    2218: "TEST",
    60: "EMU"
}

MYSEQ_MAP = {
    151: "LIVE",
    164: "TEST"
}

# to make settings.local.toml easier to read, names are added in comments
RESOURCE_NAMES = {
    "1974": "Very Vanilla MQ Live",
    "2218": "Very Vanilla MQ Test",
    "60": "Very Vanilla MQ Emu",
    "4": "KissAssist",
    "2539": "Lua Event Manager",
    "151": "MySEQ Live",
    "164": "MySEQ Test",
    "153": "Brewall's EverQuest Maps",
    "303": "Good's EverQuest Maps",
    "2318": "guildclicky",
    "2174": "buttonmaster",
    "2062": "alertmaster",
    "3040": "rgmercs",
    "2196": "lootly",
    "2088": "boxhud",
    "2391": "scriber",
    "3001": "bazaar / auction helper",
    "2937": "skill skillup: spells and others",
    "2675": "lootnscoot",
    "973": "Ninjadvloot.inc",
}

BREADCRUMB_FILENAME = "last_command.json"
DEFAULT_CONFIG_DIR = user_config_dir("redfetch", "RedGuides")

script_dir = os.path.dirname(os.path.abspath(__file__))
os.environ['REDFETCH_SCRIPT_DIR'] = script_dir

# Populated by initialize_config()
config_dir = None
env_file_path = None
settings = None


def normalize_and_create_path(path):
    if not path:
        raise ValidationError("Path is not set.")
    normalized_path = os.path.normpath(path)
    if not os.path.exists(normalized_path):
        try:
            os.makedirs(normalized_path, exist_ok=True)
            print(f"Created directory: {normalized_path}")
        except OSError as e:
            raise ValidationError(f"Failed to create the directory '{normalized_path}': {e}")
    return normalized_path


def normalize_category_paths(data):
    """Normalize and validate absolute paths in CATEGORY_PATHS."""
    if not isinstance(data, dict):
        return data
    valid_names = set(CATEGORY_MAP.values())
    for key, value in list(data.items()):
        if key not in valid_names:
            raise ValidationError(
                f"Unknown category '{key}' in CATEGORY_PATHS. "
                f"Valid categories: {', '.join(sorted(valid_names))}"
            )
        if isinstance(value, str) and value:
            normalized = os.path.normpath(value)
            data[key] = normalized
    return data


def normalize_paths_in_dict(data, parent_key=None):
    """Dynaconf validator for SPECIAL_RESOURCE paths."""
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                normalize_paths_in_dict(value, parent_key=key)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    normalize_paths_in_dict(item, parent_key=key)
            elif key in ['default_path', 'custom_path'] and isinstance(value, str):
                normalized_value = os.path.normpath(value) if value else value
                data[key] = normalized_value
    elif isinstance(data, list):
        for index, item in enumerate(data):
            normalize_paths_in_dict(item, parent_key=parent_key)
    return data


def initialize_config():
    """Initialize configuration settings."""
    from redfetch.config_firstrun import first_run_setup
    
    global config_dir, env_file_path, settings  # Declare globals to modify them

    # Perform first-run setup
    config_dir = first_run_setup()
    os.environ['REDFETCH_CONFIG_DIR'] = config_dir
    
    # Data dir: Linux default uses XDG data dir (~/.local/share), else same as config
    is_linux_default = platform.system() == "Linux" and config_dir == DEFAULT_CONFIG_DIR
    data_dir = user_data_dir("redfetch", "RedGuides") if is_linux_default else config_dir
    os.makedirs(data_dir, exist_ok=True)
    os.environ['REDFETCH_DATA_DIR'] = data_dir

    # Path to the .env file
    env_file_path = os.path.join(config_dir, '.env')

    # Check if the .env file exists
    if not os.path.exists(env_file_path):
        # If not, create it and set the default environment to 'LIVE'
        atomic_write_text(env_file_path, 'REDFETCH_ENV=LIVE\n')
        print(f".env file created with default environment set to 'LIVE' at {env_file_path}")

    # Initialize Dynaconf settings
    settings = Dynaconf(
        envvar_prefix="REDFETCH",
        settings_files=[
            os.path.join(script_dir, 'settings.toml'),
            os.path.join(config_dir, 'settings.local.toml')
        ],
        load_dotenv=True,
        dotenv_path=env_file_path,
        dotenv_override=True,
        env_switcher="REDFETCH_ENV",
        merge_enabled=True,
        lazy_load=True,
        environments=True,
        validate_on_update=True,
        validators=[
            Validator("DOWNLOAD_FOLDER", cast=normalize_and_create_path),
            # Separate validator for EQPATH to avoid triggering eqgame.exe check
            Validator("EQPATH", default=None, cast=lambda x: os.path.normpath(x) if x else None),
            Validator("SPECIAL_RESOURCES", cast=normalize_paths_in_dict),
            Validator("CATEGORY_PATHS", default={}, cast=normalize_category_paths)
        ]
    )

    write_breadcrumb()

    # Return the settings object for potential use
    return settings


def _resolve_redfetch_executable():
    """PYAPP will give a path when built with PYAPP_PASS_LOCATION=1"""
    pyapp = os.environ.get("PYAPP")
    if pyapp and "redfetch" in os.path.basename(pyapp).lower() and os.path.exists(pyapp):
        return os.path.abspath(pyapp)

    cmd = shutil.which("redfetch")
    if cmd:
        return os.path.abspath(cmd)

    return None


def atomic_write_text(path: str, text: str) -> None:
    """Write UTF-8 text to `path` via a temp file + os.replace() so readers never see a partial write."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)


def atomic_write_json(path: str, data) -> None:
    """Atomically write `data` as UTF-8 JSON (ensure_ascii=False keeps non-ASCII paths/titles verbatim)."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_breadcrumb() -> None:
    """A breadcrumb in the user config dir to track the most recently used redfetch binary's location."""
    try:
        program = _resolve_redfetch_executable()
        if program is None:
            return

        breadcrumb_path = os.path.join(DEFAULT_CONFIG_DIR, BREADCRUMB_FILENAME)
        atomic_write_json(breadcrumb_path, {"program": program})
    except Exception:
        pass


def remove_breadcrumb() -> None:
    breadcrumb_path = os.path.join(DEFAULT_CONFIG_DIR, BREADCRUMB_FILENAME)
    try:
        os.remove(breadcrumb_path)
    except FileNotFoundError:
        pass


def switch_environment(new_env):
    """Switch the environment and update the settings."""
    if settings is None:
        raise RuntimeError("Configuration has not been initialized. Call initialize_config() first.")

    # Update the .env file first
    write_env_to_file(new_env)

    # Set the Dynaconf environment so subsequent `from_env` calls use the new env
    settings.setenv(new_env)

    # Keep a simple attribute around for convenience (used throughout the app)
    settings.ENV = new_env

    # Re-validate settings after environment switch
    try:
        settings.validators.validate()
        print(f"Server type: {new_env}")
    except ValidationError as e:
        print(f"Validation error after switching to {new_env}: {e}")

    return settings


def select_environment_in_memory(new_env):
    """Select `new_env` for this process only, without persisting to the .env file."""
    if settings is None:
        raise RuntimeError("Configuration has not been initialized. Call initialize_config() first.")

    settings.setenv(new_env)
    settings.ENV = new_env

    try:
        settings.validators.validate()
    except ValidationError as e:
        print(f"Validation error after selecting {new_env}: {e}")

    return settings


def ensure_config_file_exists(file_path):
    """Ensure the configuration file exists."""
    if not os.path.exists(file_path):
        atomic_write_text(file_path, tomlkit.dumps({}))
        print(f"Created new configuration file: {file_path}")


def load_config(file_path):
    """Load the TOML configuration file, creating an empty document if it doesn't exist."""
    if not os.path.exists(file_path):
        return tomlkit.document()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return tomlkit.parse(f.read())
    except Exception as e:
        raise ValidationError(f"Error loading config file {file_path}: {e}")


def _annotate_special_resource_comments(toml_text: str) -> str:
    """Add a `# friendly-name` comment above each known SPECIAL_RESOURCES section."""
    section_pattern = re.compile(r"^\[(?:DEFAULT|LIVE|TEST|EMU)\.SPECIAL_RESOURCES\.(\d+)\]\s*$")

    new_lines = []
    for line in toml_text.splitlines():
        match = section_pattern.match(line)
        if match:
            friendly_name = RESOURCE_NAMES.get(match.group(1))
            if friendly_name and not (new_lines and new_lines[-1] == f"# {friendly_name}"):
                new_lines.append(f"# {friendly_name}")
        new_lines.append(line)

    ending = "\n" if toml_text.endswith("\n") else ""
    return "\n".join(new_lines) + ending


# Header for settings.local.toml, which redfetch rewrites on every save.
SETTINGS_LOCAL_HEADER = (
    "# Managed by redfetch: stores only your changes from settings.toml defaults.\n"
    "# Editable by hand, but redfetch rewrites on save, so comments may be dropped\n"
    "# and values matching a default are removed. See settings.toml for all options.\n"
)

# Path-valued keys, compared with path-aware equality (slash vs backslash).
_PATH_LIKE_KEYS = {"EQPATH", "DOWNLOAD_FOLDER", "custom_path", "default_path"}

_MISSING = object()
_base_settings_cache = None


def _base_settings():
    """Cached Dynaconf view of the bundled settings.toml defaults (no overrides)."""
    global _base_settings_cache
    if _base_settings_cache is None:
        _base_settings_cache = Dynaconf(
            settings_files=[os.path.join(script_dir, "settings.toml")],
            environments=True,
            merge_enabled=True,
            env_switcher="REDFETCH_ENV",
        )
    return _base_settings_cache


def _to_plain(data):
    """Convert a tomlkit document/table (or any nested mapping) to plain dicts/lists."""
    if hasattr(data, "unwrap"):
        return data.unwrap()
    if isinstance(data, dict):
        return {k: _to_plain(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_to_plain(v) for v in data]
    return data


def _base_lookup(base, key):
    """Fetch key from base defaults, tolerating Dynaconf case folding."""
    if not isinstance(base, dict):
        return _MISSING
    if key in base:
        return base[key]
    lowered = key.lower()
    for bkey, bval in base.items():
        if isinstance(bkey, str) and bkey.lower() == lowered:
            return bval
    return _MISSING


def _equals_default(value, default, key):
    """True if value matches the default (and can be dropped)."""
    if value == default:
        return True
    if (
        key in _PATH_LIKE_KEYS
        and isinstance(value, str) and value
        and isinstance(default, str) and default
    ):
        return os.path.normpath(value) == os.path.normpath(default)
    return False


def _prune_branch(local, base):
    """Recursively drop leaves equal to their default and tables left empty."""
    for key in list(local.keys()):
        value = local[key]
        base_value = _base_lookup(base, key)
        if isinstance(value, dict):
            _prune_branch(value, base_value if isinstance(base_value, dict) else {})
            if not value:
                del local[key]
        elif base_value is not _MISSING and _equals_default(value, base_value, key):
            del local[key]


def _prune_to_deltas(data):
    """Drop entries equal to the defaults, leaving only deltas.

    Each top-level table is an environment (LIVE/TEST/EMU/DEFAULT), compared
    against the same environment's defaults from the bundled settings.toml.
    """
    base = _base_settings()
    for env in list(data.keys()):
        if not isinstance(data[env], dict):
            continue
        try:
            base_env = base.from_env(env).as_dict()
        except Exception:
            continue  # defaults unresolvable; keep env verbatim
        _prune_branch(data[env], base_env)
        if not data[env]:
            del data[env]


def save_config(file_path, config_data):
    """Regenerate settings.local.toml, keeping only deltas from the defaults.

    Accepts a tomlkit document or a plain dict.
    """
    data = _to_plain(config_data)
    _prune_to_deltas(data)

    body = _annotate_special_resource_comments(tomlkit.dumps(data)).strip("\n")

    if body:
        toml_text = f"{SETTINGS_LOCAL_HEADER}\n{body}\n"
    else:
        toml_text = SETTINGS_LOCAL_HEADER
    atomic_write_text(file_path, toml_text)


def update_setting(setting_path, setting_value, env=None):
    """Update a specific setting in the settings.local.toml file and in memory,
    optionally within a specific environment."""
    if settings is None or config_dir is None:
        raise RuntimeError("Configuration has not been initialized. Call initialize_config() first.")

    config_file = os.path.join(config_dir, 'settings.local.toml')
    ensure_config_file_exists(config_file)
    config_data = load_config(config_file)

    # Use the specified environment or, if None, the current environment
    env = env or settings.current_env

    # Ensure the environment exists in the configuration
    if env not in config_data:
        config_data[env] = tomlkit.table()

    # Navigate to the correct setting based on the path within the specified environment
    current_data = config_data[env]
    for key in setting_path[:-1]:
        if key not in current_data:
            current_data[key] = tomlkit.table()
        current_data = current_data[key]

    # Debugging output
    config_key = '.'.join(setting_path)
    print(f"Updating config key: {config_key}")
    print(f"Old Value: {current_data.get(setting_path[-1], 'Not set')}")

    # Convert 'true'/'false' strings to Boolean values
    if isinstance(setting_value, str) and setting_value.lower() in ('true', 'false'):
        setting_value = setting_value.lower() == 'true'

    # None means "unset", TOML can't store None, so remove the key
    if setting_value is None:
        current_data.pop(setting_path[-1], None)
    else:
        current_data[setting_path[-1]] = setting_value

    # Update the environment using from_env to target the correct environment
    settings.from_env(env).set(config_key, setting_value)
    # Update general settings object to keep it in sync
    settings.set(config_key, setting_value)

    print(f"New Value: {setting_value}")

    save_config(config_file, config_data)
    settings.reload()

    print("Configuration saved.")


def write_env_to_file(new_env):
    """Update the environment setting in the .env file."""
    if env_file_path is None:
        raise RuntimeError("Configuration has not been initialized. Call initialize_config() first.")

    # Read the existing content of the .env file
    with open(env_file_path, 'r') as file:
        lines = file.readlines()

    # Update the environment line
    updated = False
    for i, line in enumerate(lines):
        if line.startswith('REDFETCH_ENV='):
            lines[i] = f'REDFETCH_ENV={new_env}\n'
            updated = True
            break

    # If the environment line was not found, add it
    if not updated:
        lines.append(f'REDFETCH_ENV={new_env}\n')

    # Write the updated content back to the .env file
    atomic_write_text(env_file_path, ''.join(lines))
