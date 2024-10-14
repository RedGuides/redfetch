# third-party
import requests
import keyring

# local
from .auth import KEYRING_SERVICE_NAME, authorize

def get_api_headers():

    """Fetches API details and returns the constructed headers for requests."""
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
        response = requests.get(f'https://www.redguides.com/devtestbaby/api/resources/?page={page}', headers=headers)
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
    url = 'https://www.redguides.com/devtestbaby/api/rgwatched'
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
    url = 'https://www.redguides.com/devtestbaby/api/user-licenses'
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
    url = f'https://www.redguides.com/devtestbaby/api/resources/{resource_id}'
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
    resource = fetch_single_resource(6, headers)
    return resource is not None and resource.get('can_download', False)
    
def fetch_versions_info(resource_id, headers):
    # fetch individual resource data from the API
    url = f'https://www.redguides.com/devtestbaby/api/resources/{resource_id}/versions'
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raise an exception for 4xx or 5xx status codes
    return response.json()

def get_username():
    """Fetches the username from the keyring or initiates authorization if not found."""
    username = keyring.get_password(KEYRING_SERVICE_NAME, 'username')
    if not username:
        print("Username not found. Initiating authorization process...")
        authorize()  # This will trigger the authorization process
        username = keyring.get_password(KEYRING_SERVICE_NAME, 'username')
        if not username:
            raise Exception("Authorization failed. Unable to retrieve username.")
    return username