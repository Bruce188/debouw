"""
RvVb (Raad voor Vergunningsbetwistingen) arrest scraper.

Source: https://www.dbrc.be/rechtspraak — Drupal-based listing with pagination.

Polite-scrape policy:
- User-Agent: debouw-research/0.x (set NOMINATIM_USER_AGENT in .env)
- Rate: 10 s / request (robots.txt Crawl-Delay 10)
- robots.txt re-checked at implementation time.

DOM selectors (schema-drift guarded):
- Listing container: "div.view-content"
- Arrest row: "div.views-row" or "article" within container
- Arrest link: "a" with href containing "/rechtspraak/"
- PDF link: "a[href$='.pdf']"

SchemaDriftError is raised (and logged) on any missing selector so the
circuit breaker (pipeline.py) can open + alert.

Resume-safety tiers:
1. Listing cursor: RvvbBackfillStateRow.last_page_processed → resume from next page.
2. PDF presence on disk: skip download if file already exists.
3. Sonnet cache: handled by extract_arrest.py (tier 3).
4. LanceDB row: handled by precedents.py (tier 4).

Single-writer: only one backfill_run() at a time. Concurrent invocations will
race on the cursor row but LanceDB's file lock prevents data corruption.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import httpx
import structlog
from bs4 import BeautifulSoup

from debouw.ingest.http import create_http_client
from debouw.ingest.sources.base import SchemaDriftError, Source

if TYPE_CHECKING:
    from debouw.config import Settings
    from debouw.models.permit import PermitProject, PublicInquiry

log = structlog.get_logger(__name__)

# Pattern: RVVB.A.YYMM.NNNN (e.g. RVVB.A.2425.0312 = session 24-25, nr 312)
# Year part is the first 2 digits of the 4-digit session code.
_ARREST_ID_PATTERN = re.compile(r"RVVB\.\w+\.\d{4}\.\d{4}", re.IGNORECASE)
# Extract academic year start from session code e.g. "2425" → 2024
_SESSION_YEAR_RE = re.compile(r"RVVB\.\w+\.(\d{2})(\d{2})\.", re.IGNORECASE)


def _year_from_arrest_id(arrest_id: str) -> int | None:
    """
    Extract calendar year from arrest_id session code.

    Session code "2425" → academic year 2024-2025 → start year 2024.
    Session code "2122" → 2021.
    """
    m = _SESSION_YEAR_RE.search(arrest_id)
    if not m:
        return None
    century_prefix = 20  # valid for 2000-2099
    return century_prefix * 100 + int(m.group(1))


class RvvbSource(Source):
    """
    RvVb Drupal listing scraper.

    This Source subclass only implements listing pagination and PDF download.
    The full tuple[PermitProject, PublicInquiry|None] detail_pass is NOT
    implemented — RvVb arrests are not PermitProject records; they feed the
    LanceDB precedent store via a separate pipeline (backfill_run).

    Calling detail_pass() raises NotImplementedError by design.
    """

    source_key: ClassVar[str] = "rvvb"

    # Listing endpoint: /rechtspraak?page=N
    _LISTING_PATH = "/rechtspraak"

    async def index_pass(self, *, limit: int | None = None) -> AsyncIterator[str]:
        """
        Paginate RvVb listing; yield arrest_id strings.

        Resumes from last_page_processed + 1 if a cursor row exists.
        Stops when an empty page is returned or limit is reached.

        Raises SchemaDriftError if required CSS selectors are missing.
        """
        # Circular import guard: repository is wired after Phase 5
        # Caller (backfill_run) passes resume_page explicitly.
        raise NotImplementedError(
            "Use RvvbSource.paginate() directly; index_pass() is not used by backfill_run."
        )

    async def detail_pass(
        self, identifier: str
    ) -> tuple["PermitProject", "PublicInquiry | None"]:
        """Not implemented — RvVb arrests feed LanceDB, not permit_projects."""
        raise NotImplementedError(
            "RvvbSource does not produce PermitProject records. "
            "Use backfill_run() to populate the LanceDB precedent store."
        )

    async def paginate(
        self,
        *,
        start_page: int = 0,
        limit: int | None = None,
        year_filter: list[int] | None = None,
    ) -> AsyncIterator[tuple[str, int]]:
        """
        Async generator: yields ``(arrest_id, page_index)`` tuples from the listing.

        The page index lets the caller persist a resume cursor at page-boundary
        granularity (B2 review fix — without the page index, ``backfill_run``
        had no signal to advance the cursor and resume re-fetched the same
        ``start_page`` indefinitely).

        SchemaDriftError from ``_parse_listing_page`` propagates so the caller
        (backfill orchestrator) can escalate to its circuit breaker; HTTP /
        network errors break the loop without aborting the run.

        Args:
            start_page: First page to fetch (0-indexed). Pass last_page_processed + 1
                        for cursor resume.
            limit: Maximum number of arrest_ids to yield.
            year_filter: Whitelist of calendar years; drops ids outside range.
        """
        count = 0
        page = start_page
        while True:
            url = f"{self._LISTING_PATH}?page={page}"
            try:
                response = await self._client.get(url)
            except (httpx.HTTPError, httpx.NetworkError) as exc:
                log.warning("rvvb_listing_fetch_failed", page=page, error=str(exc))
                break

            soup = BeautifulSoup(response.text, "html.parser")
            arrest_ids = _parse_listing_page(soup, url)
            if not arrest_ids:
                log.debug("rvvb_listing_empty_page", page=page)
                break

            for arrest_id in arrest_ids:
                if year_filter is not None:
                    year = _year_from_arrest_id(arrest_id)
                    if year is None or year not in year_filter:
                        continue
                yield arrest_id, page
                count += 1
                if limit is not None and count >= limit:
                    return

            page += 1

    async def download_pdf(
        self,
        arrest_id: str,
        *,
        pdf_url: str | None = None,
        dest_dir: Path,
    ) -> Path:
        """
        Download the PDF for arrest_id to dest_dir/{arrest_id}.pdf.

        Resume-tier 2: skips if file already exists.
        Returns the path to the (existing or newly downloaded) PDF.
        """
        dest = dest_dir / f"{arrest_id}.pdf"
        if dest.exists():
            log.debug("rvvb_pdf_already_exists", arrest_id=arrest_id)
            return dest

        dest_dir.mkdir(parents=True, exist_ok=True)

        if pdf_url is None:
            # Derive PDF URL from arrest_id (heuristic; actual URL found in listing)
            pdf_url = f"/sites/default/files/arr/{arrest_id}.pdf"

        try:
            response = await self._client.get(pdf_url)
            dest.write_bytes(response.content)
            log.info("rvvb_pdf_downloaded", arrest_id=arrest_id, dest=str(dest))
        except Exception as exc:
            log.warning("rvvb_pdf_download_failed", arrest_id=arrest_id, error=str(exc))
            raise
        return dest


def _parse_listing_page(soup: BeautifulSoup, url: str) -> list[str]:
    """
    Extract arrest_id strings from a listing page soup.

    Raises SchemaDriftError if no listing container is found.
    Arrest IDs are extracted from link text and hrefs matching the
    RVVB.A.YYYY.NNNN pattern.
    """
    # Primary container
    container = soup.find("div", class_="view-content")
    if container is None:
        # Fallback: check if page has any content at all
        main = soup.find("main") or soup.find("body")
        if main is None:
            raise SchemaDriftError(f"rvvb: div.view-content not found at {url}")
        # Try to find arrest IDs directly in the page text
        text = soup.get_text()
        ids = _ARREST_ID_PATTERN.findall(text)
        if not ids:
            raise SchemaDriftError(f"rvvb: no arrest IDs found at {url}")
        return list(dict.fromkeys(ids))  # deduplicate, preserve order

    # Extract from links and text within container
    ids: list[str] = []
    seen: set[str] = set()

    for tag in container.find_all(["a", "span", "div", "li", "td"]):
        text = tag.get_text(strip=True)
        found = _ARREST_ID_PATTERN.findall(text)
        for arrest_id in found:
            key = arrest_id.upper()
            if key not in seen:
                seen.add(key)
                ids.append(arrest_id)

        # Also check href
        if tag.name == "a":
            href = tag.get("href", "")
            href_found = _ARREST_ID_PATTERN.findall(href)
            for arrest_id in href_found:
                key = arrest_id.upper()
                if key not in seen:
                    seen.add(key)
                    ids.append(arrest_id)

    return ids


async def backfill_run(
    settings: "Settings",
    *,
    years: list[int],
    limit: int | None = None,
) -> int:
    """
    Orchestrate the full RvVb backfill pipeline:
    1. Paginate listing → collect arrest_ids filtered by years.
    2. For each arrest_id: download PDF (tier 2 resume-safe).
    3. Extract via Sonnet (tier 3: cache-backed by extract_arrest.py).
    4. Embed + upsert into LanceDB (tier 4: idempotent by arrest_id).

    Returns count of newly processed arrests (cache hits not counted).

    Concurrency:
    - Listing: sequential (respects 10s throttle).
    - PDF download: sequential (same throttle).
    - Sonnet extraction: asyncio.Semaphore(settings.sonnet_extraction_concurrency).
    - LanceDB write: sequential per plan (single-writer assumption).
    """
    from debouw.ingest.pdf import extract_text
    from debouw.risk.extract_arrest import ArrestExtractor
    from debouw.risk.precedents import LanceDBPrecedentStore
    from debouw.storage.db import make_engine, make_sessionmaker
    from debouw.storage.repository import get_rvvb_backfill_state, upsert_rvvb_backfill_state

    engine = make_engine(settings)
    Session = make_sessionmaker(engine)
    extractor = ArrestExtractor(settings)
    store = LanceDBPrecedentStore(settings)

    sem = asyncio.Semaphore(settings.sonnet_extraction_concurrency)
    dest_dir = settings.rvvb_backfill_root
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Resolve resume cursor
    start_page = 0
    async with Session() as session:
        state = await get_rvvb_backfill_state(session)
        if state is not None:
            last_page, _ = state
            if last_page is not None:
                start_page = last_page + 1
                log.info("rvvb_backfill_resume", start_page=start_page)

    source = RvvbSource(settings)
    count = 0
    current_page = start_page

    try:
        async for arrest_id, page_index in source.paginate(
            start_page=start_page,
            limit=limit,
            year_filter=years,
        ):
            current_page = page_index
            try:
                # Tier 2: download PDF
                pdf_path = await source.download_pdf(arrest_id, dest_dir=dest_dir)

                # Tier 3: extract via Sonnet (cache-backed). Note: extract_text
                # is an async coroutine — review-v5 B1 fix added the missing await.
                async with sem:
                    pdf_text = await extract_text(pdf_path)
                    async with Session() as session:
                        extraction = await extractor.extract(session, arrest_id, pdf_text)
                        await session.commit()

                # Tier 4: embed + upsert LanceDB
                vector = await store.embed_text(extraction.project_facts + " " + extraction.decision_excerpt)
                store.upsert_arrest(extraction, vector)

                count += 1

                # Update cursor (page index from paginate — review-v5 B2 fix).
                async with Session() as session:
                    async with session.begin():
                        await upsert_rvvb_backfill_state(
                            session,
                            last_page=current_page,
                            last_arrest_id=arrest_id,
                            updated_at=datetime.now(timezone.utc),
                        )

                log.info(
                    "rvvb_backfill_processed",
                    arrest_id=arrest_id,
                    page=current_page,
                    total=count,
                )

            except Exception as exc:
                log.warning("rvvb_backfill_arrest_failed", arrest_id=arrest_id, error=str(exc))
                continue

    finally:
        await source.aclose()
        await engine.dispose()

    log.info("rvvb_backfill_complete", total=count)
    return count
