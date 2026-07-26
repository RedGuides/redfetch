"""Tests for shared cache invalidation."""

import sqlite3

import pytest

from redfetch import auth, cache, config, meta, store


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("REDFETCH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "config_dir", None)
    original = cache._cache
    cache._cache = None
    yield
    if cache._cache is not None:
        cache._cache.close()
    cache._cache = original


def _seed_all_keys():
    auth.set_username("Redbot")
    auth.set_token_expiry("1753500000")
    cache.shared().set(f"pypi_latest:{meta.PYPI_URL}", "9.9.9")
    cache.shared().set("manifest", {"resources": []})


def test_clear_pypi_cache_leaves_identity_and_manifest(fresh_cache):
    _seed_all_keys()

    meta.clear_pypi_cache()

    assert cache.shared().get(f"pypi_latest:{meta.PYPI_URL}") is None
    assert auth.get_username_from_cache() == "Redbot"
    assert auth.get_token_expiry() == "1753500000"
    assert cache.shared().get("manifest") == {"resources": []}


def test_reset_download_dates_does_not_log_you_out(fresh_cache):
    _seed_all_keys()
    conn = sqlite3.connect(":memory:")
    store.initialize_schema(conn.cursor())

    store.reset_download_dates(conn.cursor())
    conn.close()

    assert auth.get_username_from_cache() == "Redbot"
    assert auth.get_token_expiry() == "1753500000"
    assert cache.shared().get(f"pypi_latest:{meta.PYPI_URL}") is None


def test_clear_wipes_everything_and_reopens(fresh_cache):
    _seed_all_keys()

    cache.clear()

    assert cache.shared().get("username") is None
    assert cache.shared().get("expires_at") is None
    assert cache.shared().get("manifest") is None
    assert cache.shared().get(f"pypi_latest:{meta.PYPI_URL}") is None
