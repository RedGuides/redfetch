# third-party
import requests
import keyring
import os

# local
from redfetch.auth import KEYRING_SERVICE_NAME, authorize

# Constants
BASE_URL = os.environ.get('REDFETCH_BASE_URL', 'https://www.redguides.com/community')

def get_api_headers():
    """Fetches API details and returns the constructed headers for requests."""
    api_key = os.environ.get('REDGUIDES_API_KEY')
    if api_key:
        headers = {'XF-Api-Key': api_key}
        user_id = os.environ.get('REDGUIDES_USER_ID')
        if not user_id:
            user_id = fetch_user_id_from_api(api_key)
            if not user_id:
                raise Exception("Unable to retrieve user ID using the provided API key.")
        headers['XF-Api-User'] = str(user_id)
        return headers
    else:
        # Import keyring only when needed
        api_key = keyring.get_password(KEYRING_SERVICE_NAME, 'api_key')
        user_id = keyring.get_password(KEYRING_SERVICE_NAME, 'user_id')
        if not api_key or not user_id:
            raise Exception("API key or User ID not found in keyring.")
        return {"XF-Api-Key": api_key, "XF-Api-User": str(user_id)}
    
def fetch_all_resources(headers):
    # fetch all resources from the API
    page = 1
    all_resources = []

    while True:
        response = requests.get(f'{BASE_URL}/api/resources/?page={page}', headers=headers)
        if response.ok:
            data = response.json()
            resources = data['resources']
            all_resources.extend(resources)
            if page >= data['pagination']['last_page']:
                break
            page += 1
        else:
            print(f"Error fetching resources: HTTP Status {response.status_code}")
            break

    return all_resources

def fetch_watched_resources(headers):
    """Fetches watched resources from the API with pagination."""
    url = f'{BASE_URL}/api/rgwatched'
    page = 1
    rgwatched_resources = []

    while True:
        response = requests.get(f"{url}?page={page}", headers=headers)
        if response.ok:
            data = response.json()
            # Filter to include only resources that can be downloaded and have files
            watched_resources = [
                res for res in data['resources'] 
                if res.get('can_download', False) and res.get('current_files')
            ]
            rgwatched_resources.extend(watched_resources)
            if page >= data['pagination']['last_page']:
                break
            page += 1
        else:
            print(f"Error fetching watched resources: HTTP Status {response.status_code}")
            break

    return rgwatched_resources

def fetch_licenses(headers):
    """Fetches user licenses from the API with pagination, only including licenses for downloadable resources."""
    url = f'{BASE_URL}/api/user-licenses'
    page = 1
    all_licenses = []

    while True:
        response = requests.get(f"{url}?page={page}", headers=headers)
        if response.ok:
            data = response.json()
            # Filter licenses to include only those with downloadable resources and files
            licenses = [
                lic for lic in data['licenses'] 
                if lic['resource']['can_download'] and lic['resource'].get('current_files')
            ]
            all_licenses.extend(licenses)
            if page >= data['pagination']['last_page']:
                break
            page += 1
        else:
            print(f"Error fetching licenses: HTTP Status {response.status_code}")
            break

    return all_licenses

def fetch_single_resource(resource_id, headers):
    """Fetches a single resource from the API, ensuring it is downloadable and has files."""
    url = f'{BASE_URL}/api/resources/{resource_id}'
    response = requests.get(url, headers=headers)
    if response.ok:
        resource_data = response.json()
        resource = resource_data['resource']
        if resource.get('can_download', False) and resource.get('current_files'):
            return resource  # Return only the resource details if downloadable and has files
        else:
            print(f"Resource {resource_id} is not downloadable or has no files.")
            return None
    else:
        print(f"Error fetching resource {resource_id}: HTTP Status {response.status_code}")
        return None

def fetch_single_resource_batch(resource_ids, headers):
    """Fetches single resource details for a set of resource IDs using the API. Slow."""
    resources = []
    for res_id in resource_ids:
        resource_data = fetch_single_resource(res_id, headers)
        if resource_data:
            resources.append(resource_data)
    return resources

def is_kiss_downloadable(headers):
    """Checks for level 2 access, since XF doesn't expose secondary_groups to non-admin api"""
    resource = fetch_single_resource(4, headers)
    return resource is not None and resource.get('can_download', False)
    
def fetch_versions_info(resource_id, headers):
    # fetch individual resource data from the API
    url = f'{BASE_URL}/api/resources/{resource_id}/versions'
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an exception for 4xx or 5xx status codes
    return response.json()

def fetch_user_id_from_api(api_key):
    """Fetches the user ID from the API using the provided API key."""
    url = f'{BASE_URL}/api/me'
    headers = {'XF-Api-Key': api_key}
    response = requests.get(url, headers=headers)
    if response.ok:
        user_id = response.json()['me']['user_id']
        return user_id
    else:
        print("Failed to retrieve user ID.")
        print(response.text)
        return None

def fetch_username(api_key, cache=True):
    """Fetches the username from the API using the provided API key."""
    url = f'{BASE_URL}/api/me'
    headers = {'XF-Api-Key': api_key}
    response = requests.get(url, headers=headers)
    if response.ok:
        username = response.json()['me']['username']
        if cache:
            keyring.set_password(KEYRING_SERVICE_NAME, 'username', username)
        return username
    else:
        print("Failed to retrieve username.")
        print(response.text)
        return "Unknown"

def get_username():
    """Fetches the username from the environment variable, keyring, or API."""
    # Priority 1: Environment Variable
    username = os.environ.get('REDFETCH_USERNAME')
    if username:
        return username

    # Priority 2: Keyring
    username = keyring.get_password(KEYRING_SERVICE_NAME, 'username')
    if username:
        return username

    # Priority 3: API Call
    api_key = os.environ.get('REDGUIDES_API_KEY')
    if api_key:
        username = fetch_username(api_key)
        if username != "Unknown":
            return username
        else:
            raise Exception("Unable to retrieve username using the provided API key.")

    # Priority 4: Authorization Process
    print("Username not found in environment or keyring. Initiating authorization process...")
    authorize()  # This will trigger the authorization process
    username = keyring.get_password(KEYRING_SERVICE_NAME, 'username')
    if not username:
        raise Exception("Authorization failed. Unable to retrieve username.")
    return username
