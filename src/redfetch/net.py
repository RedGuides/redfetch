"""Async HTTP utilities with retry and simple caching."""

import os
from typing import Any, Dict, Optional
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from cachetools import TTLCache
from diskcache import Cache
from redfetch import config

BASE_URL = os.environ.get("REDFETCH_BASE_URL", "https://www.redguides.com/community")
MANIFEST_URL = os.environ.get("REDFETCH_MANIFEST_URL") or f"{BASE_URL}/resources-manifest"

# Manifest cache: 5 minutes TTL
_MANIFEST_TTL_SECONDS = 300
_manifest_cache: TTLCache = TTLCache(maxsize=1, ttl=_MANIFEST_TTL_SECONDS)  # In-memory
_manifest_disk_cache: Optional[Cache] = None  # Lazy-loaded disk cache


def _get_manifest_disk_cache() -> Cache:
    """Lazy-load disk cache after config is initialized."""
    global _manifest_disk_cache
    if _manifest_disk_cache is None:
        cache_dir = getattr(config, "config_dir", None) or os.getenv("REDFETCH_CONFIG_DIR")
        if not cache_dir:
            cache_dir = os.getcwd()
        api_cache_dir = os.path.join(cache_dir, ".cache")
        _manifest_disk_cache = Cache(api_cache_dir)
    return _manifest_disk_cache


def clear_manifest_cache() -> None:
    """Clear and close both in-memory and disk manifest caches."""
    global _manifest_disk_cache
    _manifest_cache.clear()
    if _manifest_disk_cache is not None:
        try:
            _manifest_disk_cache.clear()
        finally:
            try:
                _manifest_disk_cache.close()
            except Exception:
                pass
            _manifest_disk_cache = None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(httpx.RequestError),
    reraise=True,
)
async def get_json(client: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None) -> dict:
    """GET JSON with retry on transient network errors."""
    response = await client.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


async def fetch_manifest_cached(client: httpx.AsyncClient) -> dict:
    """Fetch manifest with 5-minute cache."""
    manifest = _manifest_cache.get("manifest")
    if manifest:
        return manifest

    disk_cache = _get_manifest_disk_cache()
    manifest = disk_cache.get("manifest")
    if manifest:
        _manifest_cache["manifest"] = manifest
        return manifest

    manifest = await get_json(client, MANIFEST_URL)
    _manifest_cache["manifest"] = manifest
    disk_cache.set("manifest", manifest, expire=_MANIFEST_TTL_SECONDS)
    return manifest


async def is_mq_down_async(client: httpx.AsyncClient) -> bool:
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

        print(f"Warning: Environment {current_env} not found in status JSON.")
        return True  # Assume down if environment not found
    except (httpx.HTTPStatusError, httpx.RequestError, KeyError, ValueError) as e:
        print(f"Error fetching or parsing status: {e}")
        return True  # Assume down if there's an error


async def is_mq_down() -> bool:
    """Return True if MQ is down for current env."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await is_mq_down_async(client)

