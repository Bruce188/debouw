"""
Abstract base class for debouw scrapers.

Each source implements index_pass() (yields identifiers) and detail_pass()
(fetches + parses one dossier). The Source ABC handles HTTP client lifecycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, ClassVar, Self

from debouw.ingest.http import ThrottledHttpClient, create_http_client

if TYPE_CHECKING:
    from debouw.config import Settings
    from debouw.models.permit import PermitProject, PublicInquiry


class SchemaDriftError(Exception):
    """Raised when a scraper page is missing required HTML selectors.

    Message format: "<source>: <selector> not found at <url>"
    """


class Source(ABC):
    """Abstract base for all debouw scrapers."""

    source_key: ClassVar[str]

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._client: ThrottledHttpClient = create_http_client(settings, source=self.source_key)

    @abstractmethod
    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield detail-page identifiers (e.g. UUIDs for Gent)."""
        ...  # pragma: no cover

    @abstractmethod
    async def detail_pass(
        self, identifier: str
    ) -> tuple["PermitProject", "PublicInquiry | None"]:
        """Fetch and parse one dossier; raise SchemaDriftError on missing selectors."""
        ...  # pragma: no cover

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
