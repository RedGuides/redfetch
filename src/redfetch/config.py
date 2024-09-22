# standard
import os
# external
from dynaconf import Dynaconf, settings, Validator, ValidationError
import tomlkit

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
    
# custom dynaconf validator specifically for SPECIAL_RESOURCE paths, since keys can't be wildcarded
def normalize_paths_in_dict(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ['default_path', 'custom_path'] and isinstance(value, str):
                original_value = value  # Store original value for comparison
                data[key] = os.path.normpath(value) if value else value
            elif isinstance(value, (dict, list)):
                normalize_paths_in_dict(value)  # Recursively normalize nested structures
    elif isinstance(data, list):
        for index, item in enumerate(data):
            normalize_paths_in_dict(item)  # Recursively normalize list items
    return data
    
# Path to the .env file
env_file_path = '.env'

# Check if the .env file exists
if not os.path.exists(env_file_path):
    # If not, create it and set the default environment to 'live'
    with open(env_file_path, 'w') as env_file:
        env_file.write('REDFETCH_ENV=LIVE\n')
        print(".env file created with default environment set to 'LIVE'.")

# Initialize Dynaconf settings
settings = Dynaconf(
    envvar_prefix="REDFETCH",
    settings_files=[
        'settings.toml'
    ],
    load_dotenv=True,
    env_switcher="REDFETCH_ENV",
    merge_enabled=True,
    lazy_load=True,
    environments=True, 
    validators=[
        Validator("DOWNLOAD_FOLDER", must_exist=True, cast=os.path.normpath),
        Validator("EQPATH", default=None, cast=lambda x: os.path.normpath(x) if x else None),
        Validator("SPECIAL_RESOURCES", cast=normalize_paths_in_dict)
    ]
)
# Debugging output to see what is loaded
#print("Initial complete configuration:", settings.as_dict())
print("Server:", settings.current_env)

def switch_environment(new_env):
    """Switch the environment and update the settings."""
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

# def check_folder():
#     """This is only for the download_folder, i should either flesh this out or remove it"""
#     download_folder = settings.DOWNLOAD_FOLDER
#     if not os.path.exists(download_folder):
#         user_input = input(f"The directory '{download_folder}' does not exist. Do you want to create it? (y/n): ")
#         if user_input.lower() == 'y':
#             try:
#                 os.makedirs(download_folder, exist_ok=True)
#                 print(f"Directory '{download_folder}' created successfully.")
#             except OSError as e:
#                 print(f"Failed to create directory '{download_folder}': {e}")
#                 sys.exit("Exiting due to failure in creating directory.")
#         else:
#             print("Download folder not created. Please set a valid path.")
#             sys.exit("Exiting due to invalid directory path.")
#     else:
#         print("Download folder is valid and exists.")
            
def ensure_config_file_exists(file_path):
    """Ensure the configuration file exists."""
    if not os.path.exists(file_path):
        # If the file doesn't exist, create it with an empty TOML structure
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
    """Update a specific setting in the settings.local.toml file and in memory, optionally within a specific environment."""
    config_file = os.path.abspath('settings.local.toml')
    ensure_config_file_exists(config_file)
    config_data = load_config(config_file)

    # Use the specified environment or if None, the current environment
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

    print(f"Setting path: {'.'.join(setting_path)}")
    print(f"Old Value: {current_data.get(setting_path[-1], 'Not set')}")

    # Update the setting in the TOML data structure
    current_data[setting_path[-1]] = setting_value

    # Update the env using from_env, to target the environment
    settings.from_env(env).set('.'.join(setting_path), setting_value)
    # update general settings object. This is redundant but it keeps the settings object in sync with the from_env object.
    settings.set('.'.join(setting_path), setting_value)

    print(f"New Value: {setting_value}")
    print(f"Setting path: settings.{'.'.join(setting_path)}")

    save_config(config_file, config_data)

    print("Configuration saved.")
    print("update_setting post-configuration:", settings.as_dict())
    print("update_setting post-configuration from_env:", settings.from_env(env).as_dict())

def write_env_to_file(new_env):
    """Update the environment setting in the .env file."""
    env_file_path = '.env'
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