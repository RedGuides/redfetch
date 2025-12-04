# Standard 
import os
import re
from urllib.parse import urlparse
from pathlib import Path

# Local
from redfetch import config
from redfetch.special import is_resource_opted_in


#
# path functions
#

def get_base_path():
    """Determine the base path based on the active version."""
    # Find the vanilla mq version that corresponds to the config.settings.ENV
    active_version_key = next((k for k, v in config.VANILLA_MAP.items() if v.upper() == config.settings.ENV.upper()), None)
    if not active_version_key:
        return config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER

    # Retrieve the path
    special_path = get_special_resource_path(str(active_version_key))  # The VANILLA_MAP resource IDs are INTs but SPECIAL_RESOURCES are STRs

    return special_path if special_path else config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER


def get_folder_path(resource_id, parent_category_id, is_dependency=False, parent_resource_id=None):
    """Determine the folder path for resources and dependencies."""

    if is_dependency and parent_resource_id:
        dependency_path = get_dependency_folder_path(resource_id, parent_resource_id)
        if dependency_path:
            return os.path.normpath(dependency_path)

    # Next, check if there's a special path for this resource.
    special_path = get_special_resource_path(resource_id)
    if special_path:
        return special_path  # Already normalized in get_special_resource_path

    # If no special path given, use the base path combined with any category-specific subfolder.
    base_path = get_base_path()
    category_subfolder = config.CATEGORY_MAP.get(parent_category_id, '')
    final_path = os.path.join(base_path, category_subfolder)
    return os.path.normpath(final_path)


def ensure_directory_exists(path):
    """Ensure that the directory exists."""
    try:
        normalized_path = os.path.normpath(path)
        if not os.path.exists(normalized_path):
            os.makedirs(normalized_path, exist_ok=True)
            print(f"Created directory: {normalized_path}")
    except OSError as e:
        print(f"Error creating directory {path}: {e}")
        raise


def get_special_resource_path(resource_id):
    """Get the path for special resources."""
    special_resource = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES.get(resource_id)

    if not special_resource:
        return None

    if 'custom_path' in special_resource and special_resource['custom_path']:
        path = os.path.realpath(special_resource['custom_path'])
    elif 'default_path' in special_resource and special_resource['default_path']:
        path = os.path.join(
            config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER,
            special_resource['default_path']
        )
        # Only create the directory if the resource is opted-in
        if is_resource_opted_in(resource_id):
            ensure_directory_exists(path)
    else:
        # callers handle the case where no path is found
        return None

    # Normalize the path
    return os.path.normpath(path)


def get_dependency_folder_path(resource_id, parent_resource_id):
    """Get the folder path for a dependency resource."""
    parent_special_resource = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES.get(parent_resource_id)

    if parent_special_resource and 'dependencies' in parent_special_resource:
        dependencies = parent_special_resource['dependencies']

        # Check if the resource_id is a key in the dependencies dictionary
        if resource_id in dependencies:
            dependency_info = dependencies[resource_id]
            base_path = os.path.join(
                config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER,
                parent_special_resource.get('custom_path') or parent_special_resource.get('default_path', '')
            )
            subfolder = dependency_info.get('subfolder', '') or ''
            final_path = os.path.join(base_path, subfolder)
            return os.path.normpath(final_path)

    print("No matching dependency found or no dependencies available.")
    return None


def is_safe_path(base_directory, target_path):
    """Check for directory traversal."""
    abs_base = os.path.realpath(base_directory)
    abs_target = os.path.realpath(target_path)
    return os.path.commonpath([abs_base, abs_target]) == abs_base


def get_current_vvmq_id():
    current_env = config.settings.ENV
    for resource_id, env in config.VANILLA_MAP.items():
        if env.upper() == current_env:
            return str(resource_id)
    return None  # Return None if no matching environment is found


def get_vvmq_path():
    vvmq_id = get_current_vvmq_id()
    if vvmq_id:
        return get_special_resource_path(vvmq_id)
    return None


def get_current_myseq_id():
    current_env = config.settings.ENV
    for resource_id, env in config.MYSEQ_MAP.items():
        if env.upper() == current_env:
            return str(resource_id)
    return None  # Return None if no matching environment is found


def get_myseq_path():
    myseq_id = get_current_myseq_id()
    if myseq_id:
        return get_special_resource_path(myseq_id)
    return None  # Don't use None on select widgets


def get_ionbc_path() -> str | None:
    """Get the path to the IonBC resource, checking both the base directory and the subdirectory."""
    ionbc_id = "2463"  # The resource ID for IonBC
    base_path = get_special_resource_path(ionbc_id)
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


def get_current_download_folder():
    return os.path.normpath(config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER)


def get_eq_maps_status():
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


def parse_resource_id(input_string) -> str:
    # Check if it's already a number
    if input_string.isdigit():
        return str(input_string)

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
    """
    Validate that the given path contains a specific file.
    """
    if not path:  # If path is empty/None
        return False
    
    try:
        file_path = Path(path) / filename
        return file_path.is_file()
    except Exception:
        return False