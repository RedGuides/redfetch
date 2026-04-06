"""Resource API client: fetch_*() takes a client, get_*() is self-contained."""

import asyncio

import httpx
from pydantic import BaseModel

from redfetch import auth
from redfetch import net
from redfetch.sync_types import RemoteStatus

BASE_URL = net.BASE_URL


class ResourceRecord(BaseModel):
    """Result of a single-resource API check: status plus optional payload or error."""
    resource_id: str
    status: RemoteStatus
    resource: dict | None = None
    error: str | None = None


async def fetch_watched_page(client: httpx.AsyncClient, page: int) -> tuple[list, int]:
    """Fetch a single page of watched resources."""
    url = f'{BASE_URL}/api/rgwatched'
    try:
        data = await net.get_json(client, url, params={'page': page})
        last_page = data['pagination']['last_page']
        items = data.get('resources', [])
        return items, last_page
    except Exception as e:
        print(f"Error fetching watched resources page {page}: {e}")
        return [], 0


async def fetch_licenses_page(client: httpx.AsyncClient, page: int) -> tuple[list, int]:
    """Fetch a single page of user licenses."""
    url = f'{BASE_URL}/api/user-licenses'
    try:
        data = await net.get_json(client, url, params={'page': page})
        last_page = data['pagination']['last_page']
        items = data.get('licenses', [])
        return items, last_page
    except Exception as e:
        print(f"Error fetching licenses page {page}: {e}")
        return [], 0


async def fetch_resource_record(client: httpx.AsyncClient, resource_id: str) -> ResourceRecord:
    """Fetch a single resource and classify its availability."""
    url = f'{BASE_URL}/api/resources/{resource_id}'
    try:
        data = await net.get_json(client, url)
        resource = data['resource']
        current_files = resource.get('current_files') or []
        if not resource.get('can_download', False):
            status: RemoteStatus = 'access_denied'
        elif len(current_files) == 0:
            status = 'no_files'
        elif len(current_files) > 1:
            status = 'multiple_files'
        else:
            status = 'downloadable'
        return ResourceRecord(resource_id=resource_id, status=status, resource=resource)
    except httpx.HTTPStatusError as e:
        status = 'not_found' if e.response.status_code == 404 else 'fetch_error'
        return ResourceRecord(resource_id=resource_id, status=status, error=str(e))
    except Exception as e:
        return ResourceRecord(resource_id=resource_id, status='fetch_error', error=str(e))


async def fetch_resource_records_batch(client: httpx.AsyncClient, resource_ids: list[str]) -> list[ResourceRecord]:
    """Fetch multiple resource records concurrently."""
    if not resource_ids:
        return []
    return list(await asyncio.gather(
        *(fetch_resource_record(client, rid) for rid in resource_ids)
    ))


async def get_resource_details(resource_id: int, headers: dict) -> dict:
    """Retrieve details of a specific resource from the API."""
    url = f'{BASE_URL}/api/resources/{resource_id}'
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


_KISS_CACHE_TTL_SECONDS = 60


async def is_kiss_downloadable(headers, force_refresh: bool = False):
    """Check for level 2 access by checking KISS."""
    cache = auth.get_disk_cache()
    cache_key = "kiss"

    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return bool(cached)

    async with httpx.AsyncClient(headers=headers, http2=True) as client:
        record = await fetch_resource_record(client, "4")
    result = record.status == "downloadable"
    cache.set(cache_key, bool(result), expire=_KISS_CACHE_TTL_SECONDS)
    return result
