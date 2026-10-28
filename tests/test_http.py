"""
ThrottledHttpClient retry semantics.

Regression context: the natura-2000 WFS endpoint intermittently read-times-out;
httpx.TimeoutException is a SIBLING of httpx.NetworkError (not a subclass), so
the original predicate never retried timeouts and every slow response failed
the whole overlay on the first attempt — silently defaulting in_natura_2000
to False. All HTTP is mocked via respx. No live network calls.
"""

from __future__ import annotations

import httpx
import respx
from httpx import Response

from debouw.ingest.http import ThrottledHttpClient, _should_retry


def test_should_retry_timeouts() -> None:
    assert _should_retry(httpx.ReadTimeout("")) is True
    assert _should_retry(httpx.ConnectTimeout("")) is True
    assert _should_retry(httpx.PoolTimeout("")) is True


def test_should_retry_network_errors() -> None:
    assert _should_retry(httpx.ConnectError("boom")) is True
    assert _should_retry(httpx.ReadError("")) is True


def test_should_retry_5xx_but_not_4xx() -> None:
    req = httpx.Request("GET", "https://x.test/")
    for code, expected in [(500, True), (503, True), (404, False), (403, False)]:
        err = httpx.HTTPStatusError(
            "status", request=req, response=httpx.Response(code, request=req)
        )
        assert _should_retry(err) is expected


def test_should_not_retry_unrelated_errors() -> None:
    assert _should_retry(ValueError("nope")) is False


@respx.mock
async def test_get_retries_after_read_timeout() -> None:
    """First attempt times out, second succeeds — caller sees the 200."""
    route = respx.get("https://geo.test/wfs").mock(
        side_effect=[httpx.ReadTimeout(""), Response(200, text="ok")]
    )
    async with ThrottledHttpClient(
        base_url="https://geo.test", throttle_seconds=0.001
    ) as client:
        resp = await client.get("/wfs")
    assert resp.status_code == 200
    assert route.call_count == 2
