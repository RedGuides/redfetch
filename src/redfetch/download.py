# standard
import os
import shutil
import time
from zipfile import ZipFile

# third-party
import requests
from rich import print as rprint

# local
from redfetch import config
from redfetch.utils import (
    get_folder_path,
    is_safe_path,
)

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
    max_retries = 3
    retry_delay = 10  # seconds

    for attempt in range(max_retries):
        try:
            with zip_ref.open(member) as source, open(target_path, 'wb') as target:
                shutil.copyfileobj(source, target)
            return  # Successful extraction, exit the function
        except PermissionError:
            file_name = os.path.basename(target_path)
            folder_path = os.path.dirname(target_path)
            
            error_msg = [
                f"\nPermission Error: Unable to extract {file_name}",
                "\nThis could be because:",
                "1. The file is currently in use by another program (e.g., MacroQuest, EQBCS)",
                "2. You don't have write permissions for this location",
                "\nPossible solutions:",
                "1. Close all EverQuest-related programs (MacroQuest, EQBCS, etc.)",
                f"2. Change the installation directory in settings to a location you own",
                f"3. Manually set write permissions on: {folder_path}",
            ]
            
            if attempt < max_retries - 1:
                error_msg.append(f"\nRetrying in {retry_delay} seconds... (Attempt {attempt + 1} of {max_retries})")
                print("\n".join(error_msg))
                time.sleep(retry_delay)
            else:
                error_msg.append("\nMaximum retry attempts reached. Please resolve the permission issue and try again.")
                print("\n".join(error_msg))
                raise PermissionError(f"Failed to extract {file_name} after {max_retries} attempts.")
        except Exception as e:
            print(f"Unexpected error while extracting {os.path.basename(target_path)}: {str(e)}")
            raise

    # If we've exhausted all retries or encountered an unexpected error,
    # stop the extraction by raising an exception
    raise Exception(f"Extraction stopped due to failure extracting {os.path.basename(target_path)}.")

def delete_zip_file(zip_path):
    try:
        os.remove(zip_path)
    except PermissionError as e:
        print(f"PermissionError: Unable to delete zip file {zip_path}. Error: {e}")
        
#
# utility functions
#

def get_flatten_status(resource_id, is_dependency, parent_resource_id):
    # Does the zip want to be flattened?
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
    # Overwrite protection for specified files
    filename_lower = filename.lower()
    protected_files_lower = [f.lower() for f in protected_files]

    if filename_lower in protected_files_lower and os.path.exists(target_path):
        # Retrieve the original filename case for message consistency
        original_filename = protected_files[protected_files_lower.index(filename_lower)]
        print(f"Protected {original_filename}, skipping extraction.")
        return True
    return False