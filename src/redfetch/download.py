# standard
import os
import requests
import shutil
from zipfile import ZipFile

# local
import config

#
# download functions
#

def download_resource(resource_id, parent_category_id, download_url, filename, headers, is_dependency=False, parent_resource_id=None):
    # get the path and flatten status for this resource
    folder_path = get_folder_path(resource_id, parent_category_id, is_dependency, parent_resource_id)
    flatten = get_flatten_status(resource_id, is_dependency, parent_resource_id)

    try:
        file_path = os.path.join(folder_path, filename)
        if download_file(download_url, file_path, headers):
            if file_path.endswith('.zip'):
                extract_and_discard_zip(file_path, folder_path, resource_id, flatten)
            return True  # Indicate successful download
        else:
            print(f"Download failed for resource {resource_id}.")
            return False
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch or download resource {resource_id}: {str(e)}")
        return False
    
def download_file(download_url, file_path, headers):
    # Ensure the directory exists before downloading
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    # Perform the file download
    try:
        download_response = requests.get(download_url, headers=headers)
        download_response.raise_for_status()  # Raise an exception for 4xx or 5xx status codes
        with open(file_path, 'wb') as file:
            file.write(download_response.content)
        print(f"Downloading file {file_path}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to download file from {download_url}: {str(e)}")
        return False
    return True

#
# zip functions
#

def extract_and_discard_zip(zip_path, extract_to, resource_id, flatten=False):
    # some protection against bad zip files
    MAX_UNCOMPRESSED_SIZE = 2 * 1024 * 1024 * 1024  # 2GB limit

    # Check the compressed size as before
    zip_size = os.path.getsize(zip_path)
    if zip_size > MAX_UNCOMPRESSED_SIZE:
        print(f"ZIP file {zip_path} exceeds the 2GB size limit. Extraction aborted.")
        delete_zip_file(zip_path)
        return

    # Open the ZIP file and calculate the total uncompressed size
    with ZipFile(zip_path, 'r') as zip_ref:
        total_uncompressed_size = sum([zinfo.file_size for zinfo in zip_ref.infolist()])
        if total_uncompressed_size > MAX_UNCOMPRESSED_SIZE:
            print(f"Total uncompressed size {total_uncompressed_size} exceeds the 2GB limit. Extraction aborted.")
            delete_zip_file(zip_path)
            return

        # Load protected files for the resource
        protected_files = config.settings.from_env(config.settings.ENV).PROTECTED_FILES_BY_RESOURCE.get(resource_id, [])
        if flatten:
            extract_flattened(zip_ref, extract_to, protected_files)
        else:
            extract_with_structure(zip_ref, extract_to, protected_files)

    delete_zip_file(zip_path)

def extract_flattened(zip_ref, extract_to, protected_files):
    print(f"Flattening extraction to {extract_to}")
    for member in zip_ref.infolist():
        filename = os.path.basename(member.filename)
        if not filename:
            continue
        if is_protected(filename, extract_to, protected_files):
            print(f"Skipping protected file {filename}")
            continue
        target_path = os.path.join(extract_to, filename)
        normalized_path = os.path.normpath(target_path)
        if is_safe_path(extract_to, normalized_path):
            extract_zip_member(zip_ref, member, normalized_path)
        else:
            print(f"Skipping unsafe file {member.filename}")

def extract_with_structure(zip_ref, extract_to, protected_files):
    print(f"Extracting with structure to {extract_to}")
    for member in zip_ref.infolist():
        target_path = os.path.join(extract_to, member.filename)
        normalized_path = os.path.normpath(target_path)
        if not is_safe_path(extract_to, normalized_path):
            print(f"Skipping unsafe file {member.filename}")
            continue
        if is_protected(os.path.basename(member.filename), normalized_path, protected_files):
            print(f"Skipping protected file {member.filename}")
            continue
        if member.is_dir():
            os.makedirs(normalized_path, exist_ok=True)
            continue
        extract_zip_member(zip_ref, member, normalized_path)

def extract_zip_member(zip_ref, member, target_path):
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with zip_ref.open(member) as source, open(target_path, 'wb') as target:
        shutil.copyfileobj(source, target)
    # uncomment if you like spam
    # print(f"Extracted {member.filename} to {target_path}")

def delete_zip_file(zip_path):
    try:
        os.remove(zip_path)
    except PermissionError as e:
        print(f"PermissionError: Unable to delete zip file {zip_path}. Error: {e}")

#
# path functions
#

def get_base_path():
    """ Determine the base path based on the active version. """
    # Find the vanilla mq version that corresponds to the config.settings.ENV
    active_version_key = next((k for k, v in config.VANILLA_MAP.items() if v.upper() == config.settings.ENV.upper()), None)
    if not active_version_key:
        return config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER

    # retrieve the path
    special_path = get_special_resource_path(str(active_version_key)) # the VANILLA_MAP resource ids are INTs but SPECIAL_RESOURCES are STRs

    return special_path if special_path else config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER

def get_folder_path(resource_id, parent_category_id, is_dependency=False, parent_resource_id=None):
    """ Determine the folder path for resources and dependencies. """

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

def get_special_resource_path(resource_id):
    """ Get the path for special resources. """
    special_resource = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES.get(resource_id)

    if not special_resource:
        return None

    if 'custom_path' in special_resource and special_resource['custom_path']:
        path = os.path.realpath(special_resource['custom_path'])
    elif 'default_path' in special_resource and special_resource['default_path']:
        path = os.path.join(config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER, special_resource['default_path'])
        # making sure the default vvmq path exists for our tui dir select
        os.makedirs(path, exist_ok=True)
    else:
        # If neither path is specified, return the DOWNLOAD_FOLDER
        path = config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER

    # Normalize the path
    return os.path.normpath(path)

def get_dependency_folder_path(resource_id, parent_resource_id):
    """ Get the folder path for a dependency resource. """
    parent_special_resource = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES.get(parent_resource_id)
    
    if parent_special_resource and 'dependencies' in parent_special_resource:
        dependencies = parent_special_resource['dependencies']
        
        # Check if the resource_id is a key in the dependencies dictionary
        if resource_id in dependencies:
            dependency_info = dependencies[resource_id]
            base_path = os.path.join(config.settings.from_env(config.settings.ENV).DOWNLOAD_FOLDER, parent_special_resource.get('custom_path') or parent_special_resource.get('default_path', ''))
            subfolder = dependency_info.get('subfolder', '') or ''
            final_path = os.path.join(base_path, subfolder)
            return os.path.normpath(final_path)
    
    print("No matching dependency found or no dependencies available.")
    return None

def is_safe_path(base_directory, target_path):
    """ is that a directory traversal? """
    abs_base = os.path.realpath(base_directory)
    abs_target = os.path.realpath(target_path)
    return os.path.commonpath([abs_base, abs_target]) == abs_base

#
# utility functions
#

def get_flatten_status(resource_id, is_dependency, parent_resource_id):
    # does the zip want to be flattened?
    flatten = False
    if is_dependency and parent_resource_id:
        # Check if the parent resource has specific settings for this dependency

        parent_resource = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES.get(parent_resource_id)
        if parent_resource and 'dependencies' in parent_resource:
            dependencies = parent_resource['dependencies']
            if resource_id in dependencies:
                dependency_info = dependencies[resource_id]
                if 'flatten' in dependency_info:
                    return dependency_info['flatten']
    # Check if the resource itself has a flatten setting
    special_resource = config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES.get(resource_id)
    if special_resource and 'flatten' in special_resource:
        flatten = special_resource['flatten']
    
    return flatten

def is_protected(filename, target_path, protected_files):
    # overwrite protection for specified files
    filename_lower = filename.lower()
    protected_files_lower = [f.lower() for f in protected_files]
    
    if filename_lower in protected_files_lower and os.path.exists(target_path):
        # Retrieve the original filename case for message consistency
        original_filename = protected_files[protected_files_lower.index(filename_lower)]
        print(f"Protected {original_filename}, skipping extraction.")
        return True
    return False