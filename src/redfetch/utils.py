from . import config
import os

def is_special_or_dependency(resource_id):
    """Determine if a resource is special or a dependency, and its parent IDs."""
    is_special = is_resource_opted_in(resource_id)
    is_dependency = False
    parent_ids = []

    if is_special:
        print(f"{resource_id} is special")

    # Check if it's a dependency of any opted-in special resource
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES
    for parent_id, parent_details in special_resources.items():
        if is_resource_opted_in(parent_id):
            dependencies = parent_details.get('dependencies', {})
            if resource_id in dependencies:
                if is_dependency_opted_in(resource_id):
                    is_dependency = True
                    parent_ids.append(parent_id)
                    print(f"{resource_id} is a dependency of {parent_id}")

    return is_special, is_dependency, parent_ids

def get_opted_in_special_resources_and_dependencies():
    """Retrieve all opted-in special resources and their opted-in dependencies."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES

    resource_ids = set()
    for res_id in special_resources:
        if is_resource_opted_in(res_id):
            resource_ids.add(res_id)
            # Add opted-in dependencies
            dependencies = special_resources[res_id].get('dependencies', {})
            for dep_id in dependencies:
                if is_dependency_opted_in(dep_id):
                    resource_ids.add(dep_id)
    return resource_ids

def get_special_resource_ids_only():
    """Extracts all unique opted-in special resource IDs from special_resources, excluding dependencies."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES
    return [res_id for res_id, details in special_resources.items() if details.get('opt_in', False)]

def filter_and_fetch_dependencies(resource_ids=None):
    """Fetches opted-in resources and their dependencies."""
    if resource_ids is None:
        # Fetch all opted-in special resources and their dependencies
        resource_ids = get_opted_in_special_resources_and_dependencies()
    else:
        resource_ids = set(resource_ids)
        # Include opted-in dependencies of the provided resource IDs
        resource_ids.update(get_dependencies_for_resources(resource_ids))
    return resource_ids

def get_dependencies_for_resources(resource_ids):
    """Retrieve opted-in dependencies for the given resource IDs."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES
    dependencies = set()
    for res_id in resource_ids:
        if is_resource_opted_in(res_id):
            deps = special_resources[res_id].get('dependencies', {})
            for dep_id in deps:
                if is_dependency_opted_in(dep_id):
                    dependencies.add(dep_id)
    return dependencies

def is_resource_opted_in(resource_id):
    """Check if the given resource is opted-in."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES
    resource_details = special_resources.get(resource_id)
    return resource_details.get('opt_in', False) if resource_details else False

def is_dependency_opted_in(resource_id):
    """Check if the given resource is an opted-in dependency of any opted-in parent resource."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES

    for parent_id, parent_details in special_resources.items():
        if parent_details.get('opt_in', False):
            dependencies = parent_details.get('dependencies', {})
            dep_details = dependencies.get(resource_id)
            if dep_details and dep_details.get('opt_in', False):
                return True

    return False

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
        # If neither path is specified, return the DOWNLOAD_FOLDER
        path = config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER

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