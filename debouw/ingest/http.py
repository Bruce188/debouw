"""
Throttled async HTTP client with tenacity retry.

Adapted (not copied verbatim) from the lazy-init pattern in apex_omni_daily_trader.
No import from apex_omni — this module is standalone.
"""

from __future__ import annotations

import httpx
from asyncio_throttle import Throttler
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from debouw.config import Settings


def _should_retry(exc: BaseException) -> bool:
    """Retry on network errors and 5xx HTTP status errors."""
    if isinstance(exc, httpx.NetworkError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class ThrottledHttpClient:
    """Async HTTP client with per-source throttling and tenacity retry."""

    def __init__(
        self,
        *,
        base_url: str,
        throttle_seconds: float,
        user_agent: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._throttle_seconds = throttle_seconds
        self._user_agent = user_agent
        self._client: httpx.AsyncClient | None = None
        self._throttler = Throttler(rate_limit=1, period=throttle_seconds)
        self._retrying = AsyncRetrying(
            retry=retry_if_exception(_should_retry),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        )

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the underlying AsyncClient."""
        if self._client is None:
            headers = {}
            if self._user_agent:
                headers["User-Agent"] = self._user_agent
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                follow_redirects=True,
            )
        return self._client

    async def get(self, url: str, **kwargs) -> httpx.Response:
        """Throttled + retried GET."""
        return await self.request("GET", url, **kwargs)

    def stream(self, method: str, url: str, **kwargs):
        """Return a streaming request context manager (throttle applied inline).

        Usage:
            async with client.stream("GET", url, follow_redirects=False) as resp:
                async for chunk in resp.aiter_bytes(chunk_size):
                    ...

        Note: No tenacity retry around the stream body — caller handles aborts.
        Throttle is NOT applied here (downloads are one-shot after scrape throttle).
        """
        client = self._get_client()
        return client.stream(method, url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        """Throttled + retried POST."""
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Throttled + retried request."""
        client = self._get_client()
        async with self._throttler:
            async for attempt in self._retrying:
                with attempt:
                    response = await client.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response
        # unreachable — tenacity reraises on exhaustion
        raise RuntimeError("Retry loop exited without returning")  # pragma: no cover

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "ThrottledHttpClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()


_SOURCE_THROTTLE_MAP = {
    "gent": "throttle_gent_seconds",
    "nominatim": "throttle_nominatim_seconds",
    "rvvb": "throttle_rvvb_seconds",
    "inzageloket": "throttle_inzageloket_seconds",
    "geopunt": "throttle_geopunt_seconds",
    "onroerend_erfgoed": "throttle_geopunt_seconds",
    "brussels": "throttle_brussels_seconds",
}

_SOURCE_BASE_URL_MAP = {
    "gent": "gent_consultatie_base",
    "nominatim": "nominatim_base",
    "rvvb": "rvvb_base",
    "inzageloket": "inzageloket_base",
    "geopunt": "geopunt_base",
    "onroerend_erfgoed": "onroerend_erfgoed_base",
    "brussels": "openpermits_brussels_base",
}


def create_http_client(settings: Settings, *, source: str) -> ThrottledHttpClient:
    """Factory: build a ThrottledHttpClient configured for the given source."""
    throttle_attr = _SOURCE_THROTTLE_MAP.get(source)
    if throttle_attr is None:
        raise ValueError(f"Unknown source '{source}'. Known: {list(_SOURCE_THROTTLE_MAP)}")
    throttle_seconds: float = getattr(settings, throttle_attr)

    base_url_attr = _SOURCE_BASE_URL_MAP.get(source, "gent_consultatie_base")
    base_url: str = getattr(settings, base_url_attr, "")

    # Identified sources require a meaningful User-Agent per polite-scrape policy
    identified_sources = {"gent", "nominatim", "geopunt", "onroerend_erfgoed", "rvvb", "inzageloket", "brussels"}
    user_agent = settings.nominatim_user_agent if source in identified_sources else None

    return ThrottledHttpClient(
        base_url=base_url,
        throttle_seconds=throttle_seconds,
        user_agent=user_agent,
    )
