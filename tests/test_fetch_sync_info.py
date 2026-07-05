"""Tests for api.fetch_sync_info: pagination, accumulation, and the pre-1.5 fallback."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from redfetch import api


def _page(watched, licenses, *, has_more, is_level_2=True, is_moderator=False):
    return {
        "is_level_2": is_level_2,
        "is_moderator": is_moderator,
        "watched": watched,
        "licenses": licenses,
        "pagination": {"has_more": has_more},
    }


def _run(pages_side_effect):
    async def go():
        with patch("redfetch.api.net.get_json", new=AsyncMock(side_effect=pages_side_effect)) as mock:
            async with httpx.AsyncClient() as client:
                return await api.fetch_sync_info(client), mock
    return asyncio.run(go())


def test_single_page():
    lic = {"resource_id": 1902, "end_date": 1767225600, "subscription": True}
    info, mock = _run([_page([2, 184, 1902], [lic], has_more=False)])

    assert info is not None
    assert info.is_level_2 is True
    assert info.is_moderator is False
    assert info.watched == {"2", "184", "1902"}          # ints coerced to str ids
    assert info.licensed_ids == {"1902"}
    assert info.licenses == [lic]                          # raw rows preserved for UX
    assert mock.call_count == 1


def test_pages_until_has_more_false_and_accumulates():
    lic_a = {"resource_id": 640, "end_date": 0, "subscription": False}
    lic_b = {"resource_id": 1902, "end_date": 1767225600, "subscription": True}
    info, mock = _run([
        _page([2, 184], [lic_a], has_more=True),
        _page([1902], [lic_b], has_more=False),
    ])

    assert info.watched == {"2", "184", "1902"}
    assert info.licensed_ids == {"640", "1902"}
    assert info.licenses == [lic_a, lic_b]
    assert mock.call_count == 2
    # Second request advanced the page cursor.
    assert mock.call_args_list[1].kwargs["params"] == {"page": 2}


@pytest.mark.parametrize("status_code", [404, 500])
def test_api_errors_propagate(status_code):
    """No legacy fallback: any failure (incl. a 404 from a server without rgsync) raises."""
    request = httpx.Request("GET", "https://example.com/api/rgsync")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError("boom", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        _run([error])
