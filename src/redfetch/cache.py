"""Shared on-disk cache."""

# Standard
from contextlib import suppress

# third-party
from diskcache import Cache

# Local
from redfetch import config

_cache: Cache | None = None


def shared() -> Cache:
    """Return the shared cache."""
    global _cache
    if _cache is None:
        _cache = Cache(config.cache_dir())
    return _cache


def clear() -> None:
    """Clear and close the shared cache."""
    global _cache
    # Open it even if this process has not used it.
    store = shared()
    try:
        store.clear()
    finally:
        with suppress(Exception):
            store.close()
        _cache = None
