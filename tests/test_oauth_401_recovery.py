"""Regression tests: a bearer 401 invalidates the local token expiry and retries once."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from redfetch import net


def _run_get_json(handler, headers):
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(headers=headers, transport=transport) as client:
            return await net.get_json(client, "https://example.com/api/rgsync")
    return asyncio.run(go())


def test_bearer_401_invalidates_expiry_and_retries_with_fresh_headers():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    fresh_headers = AsyncMock(return_value={"Authorization": "Bearer replacement"})
    with (
        patch("redfetch.auth.set_token_expiry") as set_expiry,
        patch("redfetch.auth.get_api_headers", new=fresh_headers),
    ):
        result = _run_get_json(handler, {"Authorization": "Bearer rejected"})

    assert result == {"ok": True}
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer rejected",
        "Bearer replacement",
    ]
    # Zeroing the expiry is what routes get_api_headers() into its refresh path,
    # and makes authorize() fall through to full auth on the next launch.
    set_expiry.assert_called_once_with("0")


def test_second_401_raises_without_further_retries():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, request=request)

    fresh_headers = AsyncMock(return_value={"Authorization": "Bearer replacement"})
    with (
        patch("redfetch.auth.set_token_expiry"),
        patch("redfetch.auth.get_api_headers", new=fresh_headers),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        _run_get_json(handler, {"Authorization": "Bearer rejected"})

    assert exc_info.value.response.status_code == 401
    assert len(requests) == 2


def test_api_key_401_does_not_attempt_oauth_refresh():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, request=request)

    fresh_headers = AsyncMock()
    with (
        patch("redfetch.auth.get_api_headers", new=fresh_headers),
        pytest.raises(httpx.HTTPStatusError),
    ):
        _run_get_json(handler, {"XF-Api-Key": "invalid"})

    assert len(requests) == 1
    fresh_headers.assert_not_awaited()
