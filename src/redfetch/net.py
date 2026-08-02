"""Async HTTP utilities with retry and simple caching."""

import os
from typing import Any
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from redfetch import cache
from redfetch import config

BASE_URL = os.environ.get("REDFETCH_BASE_URL", "https://www.redguides.com/community")
# Manifest endpoint provided by the "Redbot - API Extensions" addon
MANIFEST_URL = f"{BASE_URL}/resources-manifest"

# Manifest cache: 60 seconds TTL
_MANIFEST_TTL_SECONDS = 60


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(httpx.RequestError),
    reraise=True,
)
async def get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> dict:
    """GET JSON with retry on transient network errors and one rejected OAuth token."""
    response = await client.get(url, params=params, timeout=10.0)
    if response.status_code == 401:
        scheme, _, token = response.request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token:
            # The server rejected a token our local expiry still trusts.
            from redfetch import auth  # lazy: auth imports this module

            auth.set_token_expiry("0")
            client.headers.update(await auth.get_api_headers())
            response = await client.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


async def fetch_manifest_cached(client: httpx.AsyncClient) -> dict:
    """Fetch manifest with a 60-second cache."""
    disk_cache = cache.shared()
    manifest = disk_cache.get("manifest")
    if manifest:
        return manifest

    manifest = await get_json(client, MANIFEST_URL)
    disk_cache.set("manifest", manifest, expire=_MANIFEST_TTL_SECONDS)
    return manifest


async def _is_mq_down_async(client: httpx.AsyncClient) -> bool:
    """Return True if MQ is down for current env."""
    url = "https://www.redguides.com/update/ready.json"
    try:
        data = await get_json(client, url)

        # Get the current environment from config settings and convert to lowercase
        current_env = config.settings.ENV.lower()

        # Check if the current environment exists in the Status dictionary (case-insensitive)
        for env, status in data["Status"].items():
            if env.lower() == current_env:
                return status.lower() != "yes"

        print(f"Warning: {current_env} not found in status JSON.")
        return True  # Assume down if environment not found
    except (httpx.HTTPStatusError, httpx.RequestError, KeyError, ValueError) as e:
        print(f"Error fetching or parsing status: {e}")
        return True  # Assume down if there's an error


async def is_mq_down() -> bool:
    """Return True if MQ is down for current env."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _is_mq_down_async(client)

