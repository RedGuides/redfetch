import config

def is_special_or_dependency(resource_id):
    """Determine if a resource is special or a dependency, and its parent IDs."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES
    is_special = False
    is_dependency = False
    parent_ids = []

    # Check if the resource is an opted-in special resource
    details = special_resources.get(resource_id)
    if details and details.get('opt_in', False):
        is_special = True
        print(f"{resource_id} is special")

    # Check if it's a dependency of any opted-in special resource
    for parent_id, parent_details in special_resources.items():
        if parent_details.get('opt_in', False):
            dependencies = parent_details.get('dependencies', {})
            dep_details = dependencies.get(resource_id)
            if dep_details and dep_details.get('opt_in', False):
                is_dependency = True
                parent_ids.append(parent_id)
                print(f"{resource_id} is a dependency of {parent_id}")

    return is_special, is_dependency, parent_ids

def get_opted_in_special_resources_and_dependencies():
    """Retrieve all opted-in special resources and their opted-in dependencies."""
    special_resources = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES

    resource_ids = set()
    for res_id, details in special_resources.items():
        if details.get('opt_in', False):
            resource_ids.add(res_id)
            # Add opted-in dependencies
            dependencies = details.get('dependencies', {})
            for dep_id, dep_details in dependencies.items():
                if dep_details.get('opt_in', False):
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
        details = special_resources.get(res_id, {})
        if details.get('opt_in', False):
            deps = details.get('dependencies', {})
            for dep_id, dep_details in deps.items():
                if dep_details.get('opt_in', False):
                    dependencies.add(dep_id)
    return dependencies