# Standard
import os
import re
from urllib.parse import urlparse

# Local
from redfetch import config

#
# path functions
#

def get_base_path() -> str:
    """Determine the base path based on the active version."""
    return get_vvmq_path() or get_current_download_folder()


def resolve_special_destination(special_resource: dict | None, download_folder: str) -> str | None:
    """Resolve a special resource destination path without side effects."""
    if not special_resource:
        return None
    custom_path = special_resource.get("custom_path")
    if custom_path:
        return os.path.normpath(os.path.realpath(custom_path))
    default_path = special_resource.get("default_path")
    if default_path:
        return os.path.normpath(os.path.join(download_folder, default_path))
    return None


def _resolve_current_special_path(resource_id: str) -> str | None:
    """Resolve the path for a special resource in the current environment."""
    settings = config.settings.from_env(config.settings.ENV)
    return resolve_special_destination(
        settings.SPECIAL_RESOURCES.get(resource_id), settings.DOWNLOAD_FOLDER
    )


def is_safe_path(base_directory: str, target_path: str) -> bool:
    """Check for directory traversal."""
    abs_base = os.path.realpath(base_directory)
    abs_target = os.path.realpath(target_path)
    return os.path.commonpath([abs_base, abs_target]) == abs_base


def get_current_vvmq_id(settings_env: str | None = None) -> str | None:
    env = (settings_env or config.settings.ENV).upper()
    for resource_id, env_name in config.VANILLA_MAP.items():
        if env_name.upper() == env:
            return str(resource_id)
    return None


def get_vvmq_path() -> str | None:
    vvmq_id = get_current_vvmq_id()
    if not vvmq_id:
        return None
    return _resolve_current_special_path(vvmq_id)


def get_current_myseq_id() -> str | None:
    current_env = config.settings.ENV.upper()
    for resource_id, env_name in config.MYSEQ_MAP.items():
        if env_name.upper() == current_env:
            return str(resource_id)
    return None


def get_myseq_path() -> str | None:
    myseq_id = get_current_myseq_id()
    if not myseq_id:
        return None
    return _resolve_current_special_path(myseq_id)


def get_ionbc_path() -> str | None:
    """Get the path to the IonBC resource, checking both the base directory and the subdirectory."""
    base_path = _resolve_current_special_path("2463")
    if not base_path:
        return None

    # Check both the base path and the subdirectory for the IonBC executable
    possible_paths = [
        base_path,
        os.path.join(base_path, "IonBC")
    ]

    for path in possible_paths:
        if os.path.exists(os.path.join(path, "IonBC.exe")):
            return path

    return None


def get_current_download_folder() -> str:
    return os.path.normpath(config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER)


def get_eq_maps_status() -> str | None:
    """Get the status of EQ maps (Brewall's and Good's)."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES
    brewall_opt_in = special_resources.get('153', {}).get('opt_in', False)
    good_opt_in = special_resources.get('303', {}).get('opt_in', False)

    if brewall_opt_in and good_opt_in:
        return "all"
    elif brewall_opt_in:
        return "brewall"
    elif good_opt_in:
        return "good"
    else:
        return None


def parse_resource_id(input_string: str) -> str:
    # Check if it's already a number
    if input_string.isdigit():
        return input_string

    # Parse the URL
    parsed_url = urlparse(input_string)

    # Check if it's a redguides.com URL
    if not parsed_url.netloc.endswith('redguides.com'):
        print(f"Invalid URL: Neither a redguides.com URL nor a valid resource id")
        raise ValueError("Invalid URL: Neither a redguides.com URL nor a valid resource id")

    # Check if it's a thread URL
    if 'threads' in parsed_url.path:
        print(f"Invalid URL: This appears to be a discussion thread, not a resource")
        raise ValueError("Invalid URL: This appears to be a discussion thread, not a resource")

    # Extract the resource ID using regex
    match = re.search(r'\.(\d+)(?:/|$)', parsed_url.path)
    if match:
        return str(match.group(1))
    else:
        print(f"Could not find a valid resource ID in the URL")
        raise ValueError("Could not find a valid resource ID in the URL")


def validate_file_in_path(path: str | None, filename: str) -> bool:
    """Validate that the given path contains a specific file."""
    if not path:
        return False
    try:
        return os.path.isfile(os.path.join(path, filename))
    except (TypeError, ValueError):
        return False