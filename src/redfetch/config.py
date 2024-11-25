# standard
import os

# third-party
import tomlkit
from dynaconf import Dynaconf, Validator, ValidationError

# local
from redfetch.config_firstrun import first_run_setup

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

EQMAPS_MAP = {
    153: "Brewall",
    303: "Goods"
}

def validate_no_eqgame(path):
    """Validate that the path and its parents don't contain eqgame.exe."""
    current_path = os.path.abspath(path)
    while current_path != os.path.dirname(current_path):  # Stop at root
        if os.path.exists(os.path.join(current_path, 'eqgame.exe')):
            raise ValidationError(f"Path '{path}' or its parent contains eqgame.exe")
        current_path = os.path.dirname(current_path)

def normalize_and_create_path(path):
    if not path:
        raise ValidationError("Path is not set.")
    normalized_path = os.path.normpath(path)
    validate_no_eqgame(normalized_path)
    if not os.path.exists(normalized_path):
        try:
            os.makedirs(normalized_path, exist_ok=True)
            print(f"Created directory: {normalized_path}")
        except OSError as e:
            raise ValidationError(f"Failed to create the directory '{normalized_path}': {e}")
    return normalized_path

# Custom Dynaconf validator specifically for SPECIAL_RESOURCE paths
def normalize_paths_in_dict(data, parent_key=None):
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                normalize_paths_in_dict(value, parent_key=key)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    normalize_paths_in_dict(item, parent_key=key)
            elif key in ['default_path', 'custom_path'] and isinstance(value, str):
                normalized_value = os.path.normpath(value) if value else value
                parent_key_int = int(parent_key) if isinstance(parent_key, str) and parent_key.isdigit() else parent_key
                if parent_key_int not in EQMAPS_MAP:
                    validate_no_eqgame(normalized_value)
                data[key] = normalized_value
    elif isinstance(data, list):
        for index, item in enumerate(data):
            normalize_paths_in_dict(item, parent_key=parent_key)
    return data

# Initialize variables
script_dir = os.path.dirname(os.path.abspath(__file__))
os.environ['REDFETCH_SCRIPT_DIR'] = script_dir

# Declare these variables; they will be initialized in initialize_config()
config_dir = None
env_file_path = None
settings = None

def initialize_config():
    """Initialize configuration settings."""
    global config_dir, env_file_path, settings  # Declare globals to modify them

    # Perform first-run setup
    config_dir = first_run_setup()
    os.environ['REDFETCH_CONFIG_DIR'] = config_dir

    # Path to the .env file
    env_file_path = os.path.join(config_dir, '.env')

    # Check if the .env file exists
    if not os.path.exists(env_file_path):
        # If not, create it and set the default environment to 'LIVE'
        with open(env_file_path, 'w') as env_file:
            env_file.write('REDFETCH_ENV=LIVE\n')
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
            Validator("SPECIAL_RESOURCES", cast=normalize_paths_in_dict)
        ]
    )

    # Return the settings object for potential use
    return settings

def switch_environment(new_env):
    """Switch the environment and update the settings."""
    if settings is None:
        raise RuntimeError("Configuration has not been initialized. Call initialize_config() first.")

    # Update the .env file first
    write_env_to_file(new_env)

    # Now set the environment
    settings.setenv(new_env)
    settings.from_env(new_env).setenv(new_env)

    # Explicitly set the ENV variable if needed
    settings.ENV = new_env

    # Update the from_env object to reflect the new environment
    settings.from_env(new_env).ENV = new_env

    # Re-validate settings after environment switch
    try:
        settings.validators.validate()
        print(f"Server type: {new_env}")
    except ValidationError as e:
        print(f"Validation error after switching to {new_env}: {e}")

    return settings

def ensure_config_file_exists(file_path):
    """Ensure the configuration file exists."""
    if not os.path.exists(file_path):
        # If the file doesn't exist, create it with an empty TOML structure
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w') as f:
            f.write(tomlkit.dumps({}))  # Create an empty TOML file
        print(f"Created new configuration file: {file_path}")

def load_config(file_path):
    """Load the TOML configuration file."""
    with open(file_path, 'r') as f:
        return tomlkit.parse(f.read())

def save_config(file_path, config_data):
    """Save the updated configuration data to the TOML file."""
    with open(file_path, 'w') as f:
        f.write(tomlkit.dumps(config_data))

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

    # Update the setting in the TOML data structure
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
    with open(env_file_path, 'w') as file:
        file.writelines(lines)
