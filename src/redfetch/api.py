"""API client for RedGuides with async HTTP support."""
import asyncio
import os
from typing import List, Optional

import httpx
import keyring
from diskcache import Cache

from redfetch.auth import KEYRING_SERVICE_NAME
from redfetch import config
from redfetch import net

# Constants
BASE_URL = net.BASE_URL


async def get_api_headers():
    """Fetch API key/user headers for authenticated API requests."""
    api_key = os.environ.get('REDGUIDES_API_KEY')
    if api_key:
        headers = {'XF-Api-Key': api_key}
        user_id = os.environ.get('REDGUIDES_USER_ID')
        if not user_id:
            user_id = await fetch_user_id_from_api(api_key)
            if not user_id:
                raise Exception("Unable to retrieve user ID using the provided API key.")
        headers['XF-Api-User'] = str(user_id)
        return headers
    
    api_key = keyring.get_password(KEYRING_SERVICE_NAME, 'api_key')
    if not api_key:
        raise Exception("API key not found. Please run authorization.")
    
    user_id = get_user_id()
    if not user_id:
        user_id = await fetch_user_id_from_api(api_key)
        if not user_id:
            raise Exception("Unable to retrieve user ID. Please re-authorize.")
    
    return {"XF-Api-Key": api_key, "XF-Api-User": str(user_id)}


async def fetch_watched_page(client: httpx.AsyncClient, page: int) -> tuple[list, int]:
    """Fetch a single page of watched resources.
    
    Returns: (resources, total_pages)
    """
    url = f'{BASE_URL}/api/rgwatched'
    try:
        data = await net.get_json(client, url, params={'page': page})
        last_page = data['pagination']['last_page']
        items = [res for res in data['resources'] if res.get('can_download', False) and res.get('current_files')]
        return items, last_page
    except Exception as e:
        print(f"Error fetching watched resources page {page}: {e}")
        return [], 0


async def fetch_licenses_page(client: httpx.AsyncClient, page: int) -> tuple[list, int]:
    """Fetch a single page of user licenses.
    
    Returns: (licenses, total_pages)
    """
    url = f'{BASE_URL}/api/user-licenses'
    try:
        data = await net.get_json(client, url, params={'page': page})
        last_page = data['pagination']['last_page']
        items = [lic for lic in data['licenses'] if lic['resource']['can_download'] and lic['resource'].get('current_files')]
        return items, last_page
    except Exception as e:
        print(f"Error fetching licenses page {page}: {e}")
        return [], 0


async def fetch_single_resource(client: httpx.AsyncClient, resource_id: str) -> Optional[dict]:
    """Fetches a single resource from the API, ensuring it is downloadable and has files."""
    url = f'{BASE_URL}/api/resources/{resource_id}'
    try:
        data = await net.get_json(client, url)
        resource = data['resource']
        if resource.get('can_download', False) and resource.get('current_files'):
            return resource
        else:
            print(f"Resource {resource_id} is not downloadable or has no files.")
            return None
    except httpx.HTTPStatusError as e:
        print(f"Error fetching resource {resource_id}: HTTP Status {e.response.status_code}")
        return None
    except Exception as e:
        print(f"Error fetching resource {resource_id}: {e}")
        return None


async def get_resource_details(resource_id: int) -> dict:
    """Retrieve details of a specific resource from the API."""
    url = f'{BASE_URL}/api/resources/{resource_id}'
    headers = await get_api_headers()
    async with httpx.AsyncClient(headers=headers, http2=True, timeout=30.0) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.json()['resource']


async def fetch_watched_resources(client: httpx.AsyncClient) -> list:
    """Fetch all watched resources with concurrent pagination."""
    items, total_pages = await fetch_watched_page(client, 1)
    if total_pages <= 1:
        return items

    coros = [fetch_watched_page(client, p) for p in range(2, total_pages + 1)]
    page_results = await asyncio.gather(*coros)

    for page_items, _ in page_results:
        items.extend(page_items)
    
    return items


async def fetch_licenses(client: httpx.AsyncClient) -> list:
    """Fetch all user licenses with concurrent pagination."""
    items, total_pages = await fetch_licenses_page(client, 1)
    if total_pages <= 1:
        return items
    coros = [fetch_licenses_page(client, p) for p in range(2, total_pages + 1)]
    page_results = await asyncio.gather(*coros)

    for page_items, _ in page_results:
        items.extend(page_items)
    
    return items


async def fetch_resources_batch(client: httpx.AsyncClient, resource_ids: List[str]) -> list:
    """Fetch multiple resources concurrently; returns list of resource dicts."""
    if not resource_ids:
        return []
    coroutines = [fetch_single_resource(client, rid) for rid in resource_ids]
    responses = await asyncio.gather(*coroutines, return_exceptions=True)

    results = []
    for rid, data in zip(resource_ids, responses):
        if isinstance(data, Exception):
            print(f"Warning: Resource {rid} failed to fetch: {data}")
            continue
        if data:
            results.append(data)
    
    return results


_KISS_CACHE_TTL_SECONDS = 60  # 60 seconds


# Persistent disk-backed cache (survives across CLI runs)
def _get_api_cache():
    """Lazy-load cache after config is initialized."""
    cache_dir = getattr(config, 'config_dir', None) or os.getenv('REDFETCH_CONFIG_DIR')
    if not cache_dir:
        cache_dir = os.getcwd()
    api_cache_dir = os.path.join(cache_dir, '.cache')
    return Cache(api_cache_dir)


_api_cache = None


def set_user_id(user_id: str) -> None:
    """Store user_id in disk cache (non-sensitive public identifier)."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    _api_cache.set('user_id', str(user_id))


def get_user_id() -> Optional[str]:
    """Retrieve user_id from disk cache."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    return _api_cache.get('user_id')


def set_username(username: str) -> None:
    """Store username in disk cache (non-sensitive public display name)."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    _api_cache.set('username', username)


def get_username_from_cache() -> Optional[str]:
    """Retrieve username from disk cache."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    return _api_cache.get('username')


def set_token_expiry(expires_at: str) -> None:
    """Store OAuth token expiry timestamp in disk cache."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    _api_cache.set('expires_at', expires_at)


def get_token_expiry() -> Optional[str]:
    """Retrieve OAuth token expiry timestamp from disk cache."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    return _api_cache.get('expires_at')


async def is_kiss_downloadable(headers, force_refresh: bool = False):
    """Check for level 2 access by checking KISS."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    
    # Single-user install: use a singleton cache key
    cache_key = "kiss"
    
    if not force_refresh:
        cached = _api_cache.get(cache_key)
        if cached is not None:
            return bool(cached)

    async with httpx.AsyncClient(headers=headers, http2=True) as client:
        resource = await fetch_single_resource(client, "4")
    result = resource is not None and resource.get('can_download', False)
    _api_cache.set(cache_key, bool(result), expire=_KISS_CACHE_TTL_SECONDS)
    return result


async def fetch_me(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch current user info from /api/me."""
    url = f'{BASE_URL}/api/me'
    try:
        data = await net.get_json(client, url)
        return {
            'user_id': str(data['me']['user_id']),
            'username': data['me']['username']
        }
    except Exception as e:
        print(f"Failed to retrieve user info: {e}")
        return None


async def fetch_user_id_from_api(api_key):
    """Fetch user_id using the API key; caches it."""
    async with httpx.AsyncClient(headers={'XF-Api-Key': api_key}, http2=True) as client:
        me = await fetch_me(client)
    if me:
        set_user_id(me['user_id'])
        return me['user_id']
    return None


async def fetch_username(api_key, cache=True):
    """Fetch username via API key; caches username and user_id."""
    async with httpx.AsyncClient(headers={'XF-Api-Key': api_key}, http2=True) as client:
        me = await fetch_me(client)
    if me:
        if cache:
            set_username(me['username'])
            set_user_id(me['user_id'])
        return me['username']
    return "Unknown"


def clear_api_cache():
    """Clear all cached API data."""
    global _api_cache
    if _api_cache is None:
        _api_cache = _get_api_cache()
    cache = _api_cache
    try:
        cache.clear()
    finally:
        try:
            cache.close()
        except Exception:
            pass
        _api_cache = None


async def get_username():
    """Fetch the username from the environment variable, disk cache, or API."""
    username = os.environ.get('REDFETCH_USERNAME')
    if username:
        return username

    username = get_username_from_cache()
    if username:
        return username

    api_key = os.environ.get('REDGUIDES_API_KEY')
    if api_key:
        username = await fetch_username(api_key)
        if username != "Unknown":
            return username
        raise Exception("Unable to retrieve username using the provided API key.")

    # Check keyring for API key (used after OAuth authorization)
    api_key = keyring.get_password(KEYRING_SERVICE_NAME, 'api_key')
    if api_key:
        username = await fetch_username(api_key)
        if username != "Unknown":
            return username
        raise Exception("Unable to retrieve username using the stored API key.")

    raise Exception("Username not found. Please run authorization first.")
