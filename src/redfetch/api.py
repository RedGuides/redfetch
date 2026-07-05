"""Resource API client: fetch_*() takes a client, get_*() is self-contained."""

import httpx
from redfetch import net
from redfetch.sync_types import SyncInfo

BASE_URL = net.BASE_URL


async def fetch_sync_info(client: httpx.AsyncClient) -> SyncInfo:
    """Per-user sync info"""
    url = f'{BASE_URL}/api/rgsync'
    is_level_2 = is_moderator = False
    watched: set[str] = set()
    licenses: list[dict] = []
    page = 1
    while True:
        data = await net.get_json(client, url, params={'page': page})
        is_level_2 = bool(data.get('is_level_2', False))
        is_moderator = bool(data.get('is_moderator', False))
        watched.update(str(rid) for rid in data.get('watched', []))
        licenses.extend(data.get('licenses', []))
        if not data.get('pagination', {}).get('has_more', False):
            break
        page += 1

    licensed_ids = {
        str(lic['resource_id']) for lic in licenses if lic.get('resource_id') is not None
    }
    return SyncInfo(
        is_level_2=is_level_2,
        is_moderator=is_moderator,
        watched=watched,
        licensed_ids=licensed_ids,
        licenses=licenses,
    )


async def get_sync_info(headers: dict) -> SyncInfo:
    """fetch_sync_info for callers that hold headers but not a client."""
    async with httpx.AsyncClient(headers=headers, http2=True, timeout=30.0) as client:
        return await fetch_sync_info(client)


async def get_resource_details(resource_id: int, headers: dict) -> dict:
    """Retrieve details of a specific resource from the API."""
    url = f'{BASE_URL}/api/resources/{resource_id}'
    async with httpx.AsyncClient(headers=headers, http2=True, timeout=30.0) as client:
        data = await net.get_json(client, url)
    return data['resource']
