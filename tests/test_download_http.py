"""HTTP-level behavior of download_file_async: retry policy and the real streaming path."""
import asyncio

import httpx
import pytest

from redfetch import download


def _run_download(handler, dest, **client_kwargs):
    """Drive download_file_async through a MockTransport, capturing every request."""
    requests: list[httpx.Request] = []

    def recording(request):
        requests.append(request)
        return handler(request)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recording), **client_kwargs
        ) as client:
            return await download.download_file_async(
                client, "https://host.example.test/file.bin", str(dest)
            )

    return go, requests


def test_permanent_403_is_not_retried(tmp_path):
    """A retried 4xx re-sends a byte-identical request, so it can only fail the same way."""
    go, requests = _run_download(
        lambda r: httpx.Response(403, headers={"cf-mitigated": "challenge"}),
        tmp_path / "p.zip",
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(go())
    assert len(requests) == 1
    # the signal the patcher bootstrap's error message reads
    assert excinfo.value.response.status_code == 403
    assert excinfo.value.response.headers.get("cf-mitigated") == "challenge"


@pytest.mark.parametrize("code", [503, 429])
def test_transient_statuses_still_retry(tmp_path, code):
    go, requests = _run_download(lambda r: httpx.Response(code), tmp_path / "s.bin")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())
    assert len(requests) == 3


def test_download_streams_and_swaps_through_a_real_transport(tmp_path):
    payload = b"\x00\x01" * 550
    dest = tmp_path / "sub" / "x.bin"
    go, requests = _run_download(lambda r: httpx.Response(200, content=payload), dest)
    assert asyncio.run(go()) is True
    assert dest.read_bytes() == payload
    assert len(requests) == 1


def test_redirects_follow_regardless_of_client_flag(tmp_path):
    """Per-request follow_redirects=True wins over the client's own setting."""
    def handler(request):
        if request.url.path == "/file.bin":
            return httpx.Response(302, headers={"location": "https://cdn.example.test/real.bin"})
        return httpx.Response(200, content=b"cdn-bytes")

    dest = tmp_path / "r.bin"
    go, requests = _run_download(handler, dest, follow_redirects=False)
    assert asyncio.run(go()) is True
    assert dest.read_bytes() == b"cdn-bytes"
    assert len(requests) == 2
