# Standard 
import os
import json
import re
from urllib.parse import urlparse
import subprocess
import sys
from pathlib import Path

# Third-party
import requests
from rich.prompt import Prompt, InvalidResponse
import psutil

# Local
from redfetch import config

if sys.platform == 'win32':
    from .unloadmq import force_remote_unload
else:
    def force_remote_unload():
        pass  # No operation on non-Windows platforms

class TerminationPrompt(Prompt):
    """Custom prompt to ask the user about terminating processes, including 'Always' and 'Never' options."""
    response_type = str
    validate_error_message = "[prompt.invalid]Please enter yes, no, always, or never"
    choices = ["yes", "no", "always", "never"]
    complete_style = "default"

    def process_response(self, value: str) -> str:
        """Process the user's response."""
        value = value.strip().lower()
        if value in ['y', 'yes']:
            return "yes"
        elif value in ['n', 'no']:
            return "no"
        elif value == 'always':
            return "always"
        elif value == 'never':
            return "never"
        else:
            raise InvalidResponse(self.validate_error_message)

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

def is_mq_down():
    """Check if MQ is down based on the status from the ready.json file."""
    url = "https://www.redguides.com/update/ready.json"
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad status codes
        data = response.json()
        
        # Get the current environment from config settings and convert to lowercase
        current_env = config.settings.ENV.lower()
        
        # Check if the current environment exists in the Status dictionary (case-insensitive)
        for env, status in data["Status"].items():
            if env.lower() == current_env:
                return status.lower() != "yes"
        
        print(f"Warning: Environment {current_env} not found in status JSON.")
        return True  # Assume down if environment not found
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"Error fetching or parsing status: {e}")
        return True  # Assume down if there's an error

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
    return None # Return None if no matching environment is found
    
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
    
def parse_resource_id(input_string):
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
        return int(match.group(1))
    else:
        print(f"Could not find a valid resource ID in the URL")
        raise ValueError("Could not find a valid resource ID in the URL")

def are_executables_running_in_folder(folder_path):
    """
    Check if any .exe files in the specified folder are currently running processes.

    Returns a list of running executables in the folder.
    """
    if not sys.platform == 'win32':
        return []  # Return empty list for non-Windows platforms
        
    running_executables = []
    try:
        # Get the absolute, normalized path of the folder
        folder_path = os.path.normpath(os.path.abspath(folder_path))
        # List all .exe files in the folder
        exe_files = [os.path.normcase(os.path.join(folder_path, f)) for f in os.listdir(folder_path) if f.lower().endswith('.exe')]
        if not exe_files:
            print(f"No executable files found in {folder_path}")
            return running_executables

        # Iterate over all running processes
        for proc in psutil.process_iter(['pid', 'exe', 'name']):
            try:
                exe_path = proc.info['exe']
                if exe_path and os.path.isfile(exe_path):
                    # Normalize the path for comparison
                    exe_path_normalized = os.path.normcase(os.path.normpath(exe_path))
                    if exe_path_normalized in exe_files:
                        print(f"Process '{exe_path}' (PID {proc.pid}) is currently running.")
                        running_executables.append((proc.pid, exe_path))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        return running_executables
    except Exception as e:
        print(f"An error occurred while checking running processes: {e}")
        return running_executables

def terminate_executables_in_folder(folder_path):
    """
    Attempt to terminate any running .exe files in the specified folder and then unload MacroQuest.
    Only works on Windows, does nothing on other platforms.
    """
    if sys.platform != 'win32':
        print("Terminating executables is only supported on Windows platforms.")
        return

    try:
        # Get the absolute, normalized path of the folder
        folder_path = os.path.normpath(os.path.abspath(folder_path))
        # List all .exe files in the folder
        exe_files = [os.path.normcase(os.path.join(folder_path, f)) for f in os.listdir(folder_path) if f.lower().endswith('.exe')]
        if not exe_files:
            print(f"No executable files found in {folder_path}")
            return

        # Iterate over all running processes
        for proc in psutil.process_iter(['pid', 'exe', 'name']):
            try:
                exe_path = proc.info['exe']
                if exe_path and os.path.isfile(exe_path):
                    # Normalize the path for comparison
                    exe_path_normalized = os.path.normcase(os.path.normpath(exe_path))
                    if exe_path_normalized in exe_files:
                        # Terminate the process
                        proc.terminate()
                        proc.wait(timeout=5)
                        print(f"Terminated process '{exe_path}' (PID {proc.pid}).")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
                print(f"Could not terminate process: {e}")

        # Unload MacroQuest after terminating executables
        try:
            force_remote_unload()
        except Exception as e:
            print(f"Error unloading MacroQuest: {e}")

    except Exception as e:
        print(f"An error occurred while terminating processes: {e}")

def run_executable(folder_path: str, executable_name: str, args=None) -> bool:
    """Run an executable from a specified folder. Use args if you need to pass arguments to the executable."""
    if not sys.platform.startswith('win'):
        print("Running executables is only supported on Windows.")
        return False

    if not folder_path:
        print(f"Folder path not set for {executable_name}")
        return False

    executable_path = os.path.join(folder_path, executable_name)
    if os.path.isfile(executable_path):
        try:
            if args is None:
                args = []
            subprocess.Popen([executable_path] + args, cwd=folder_path)
            print(f"{executable_name} started successfully.")
            return True
        except Exception as e:
            print(f"Failed to start {executable_name}: {e}")
            return False
    else:
        print(f"{executable_name} not found in the specified folder.")
        return False

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