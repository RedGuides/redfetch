"""Endpoint test for the local web interface API."""
import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from redfetch import config, listener


class FakeSettings:
    """Two-env settings double exercising the from_env(ENV) path."""

    def __init__(self):
        self.ENV = "LIVE"
        self._envs = {
            "LIVE": {"1974": {"opt_in": True}, "153": {"opt_in": False}, "60": {"opt_in": True}},
            "TEST": {"2218": {"opt_in": True}},
        }

    def from_env(self, env):
        return SimpleNamespace(SPECIAL_RESOURCES=self._envs[env])


def test_special_resource_ids_follow_runtime_env(monkeypatch):
    """GET /special-resource-ids returns opted-in ids as ints and tracks runtime ENV swaps (--server)."""
    fake = FakeSettings()
    monkeypatch.setattr(config, "settings", fake)

    async def fetch_ids():
        app = await listener.create_app(settings=None, db_name="LIVE", headers={}, category_map={})
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/special-resource-ids")
            assert resp.status == 200
            return await resp.json()

    assert sorted(asyncio.run(fetch_ids())) == [60, 1974]

    fake.ENV = "TEST"
    assert asyncio.run(fetch_ids()) == [2218]
