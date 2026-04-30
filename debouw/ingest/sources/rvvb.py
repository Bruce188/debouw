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

    # Listing endpoint with document_type=49 facet (RVVB.A arresten only).
    # The unfiltered /rechtspraak?page=N returns mixed RVVB.A + RVVB.S +
    # RVERKB rows; the facet narrows to "arresten" — F4's target corpus.
    _LISTING_PATH = "/rechtspraak?f%5B0%5D=document_type%3A49"

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
    ) -> AsyncIterator[tuple[str, int, str | None]]:
        """
        Async generator: yields ``(arrest_id, page_index, pdf_url)`` triples
        from the listing.

        ``pdf_url`` is the absolute href found alongside the arrest_id in the
        listing HTML (e.g.
        ``https://www.dbrc.be/sites/default/files/2026-04/RVVB.A.2526.0625.pdf``);
        it may be ``None`` if the arrest_id was extracted from page text without
        an adjacent PDF link. ``download_pdf`` accepts the absolute form.

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
        # Path may already carry a query string (document_type facet); pick separator.
        sep = "&" if "?" in self._LISTING_PATH else "?"
        # Listing fetch retry: transient httpx errors (timeouts, 5xx) early-end
        # an otherwise-deep paginated run. Retry the same page up to 3 times
        # with exponential backoff before giving up — observed once mid-run on
        # page 45 with an empty error string.
        _MAX_LISTING_RETRIES = 3
        while True:
            url = f"{self._LISTING_PATH}{sep}page={page}"
            response = None
            for attempt in range(_MAX_LISTING_RETRIES):
                try:
                    response = await self._client.get(url)
                    break
                except (httpx.HTTPError, httpx.NetworkError) as exc:
                    log.warning(
                        "rvvb_listing_fetch_retry",
                        page=page,
                        attempt=attempt + 1,
                        error=str(exc),
                    )
                    if attempt + 1 == _MAX_LISTING_RETRIES:
                        log.warning(
                            "rvvb_listing_fetch_failed", page=page, error=str(exc)
                        )
                        response = None
                        break
                    await asyncio.sleep(2 ** (attempt + 1))  # 2s, 4s
            if response is None:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            entries = _parse_listing_with_pdfs(soup, url)
            if not entries:
                log.debug("rvvb_listing_empty_page", page=page)
                break

            for arrest_id, pdf_url in entries:
                # Normalise the arrest_id case: dbrc.be has mixed-case PDF
                # URLs (some rows ``RVVB.A.2425.0670``, some
                # ``rvvb.a.2425.0668`` on the same listing page). The
                # downstream LanceDB key + ``ArrestExtraction`` validator
                # require canonical UPPERCASE; preserve the original ``pdf_url``
                # so the on-disk PDF still downloads from the lowercase href.
                arrest_id = arrest_id.upper()
                if year_filter is not None:
                    year = _year_from_arrest_id(arrest_id)
                    if year is None or year not in year_filter:
                        continue
                yield arrest_id, page, pdf_url
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
            # Listing href is the canonical PDF URL — `dbrc.be` stores PDFs in
            # month-stamped folders ``/sites/default/files/YYYY-MM/…`` rather
            # than a flat directory, so callers must pass the parsed href.
            raise ValueError(
                f"rvvb: pdf_url required for {arrest_id}; "
                "no flat-path heuristic exists for dbrc.be."
            )

        try:
            response = await self._client.get(pdf_url)
            dest.write_bytes(response.content)
            log.info("rvvb_pdf_downloaded", arrest_id=arrest_id, dest=str(dest))
        except Exception as exc:
            log.warning("rvvb_pdf_download_failed", arrest_id=arrest_id, error=str(exc))
            raise
        return dest


def _parse_listing_with_pdfs(
    soup: BeautifulSoup, url: str
) -> list[tuple[str, str | None]]:
    """
    Extract ``(arrest_id, pdf_url)`` pairs from a listing page soup.

    Container-agnostic: live ``dbrc.be`` wraps results in
    ``<div class="view view-legislation-search …">`` with article cards,
    while older test fixtures used ``<div class="view-content">``. Rather than
    chase the Drupal theme version, we scan the whole document for two signals:

    1. Any ``<a href="…RVVB.A.NNNN.NNNN.pdf">`` — harvest the absolute URL
       so the caller can pass it straight to ``download_pdf`` instead of
       guessing the month folder (PDFs live at ``/sites/default/files/YYYY-MM/``).
    2. Any element text containing the arrest_id pattern — covers fallback
       cases where the listing renders the ID inline without an adjacent
       PDF anchor.

    Raises SchemaDriftError if no arrest IDs are found anywhere on the page
    (preserves the original drift-guard contract).
    """
    pdf_by_id: dict[str, str] = {}

    # Pass 1: harvest pdf_urls from any <a href="*.pdf"> with arrest_id in URL.
    for a in soup.find_all("a"):
        href_raw = a.get("href", "")
        href = str(href_raw) if href_raw else ""
        if ".pdf" not in href.lower():
            continue
        href_ids = _ARREST_ID_PATTERN.findall(href)
        if not href_ids:
            continue
        arrest_id = href_ids[0]
        key = arrest_id.upper()
        if key not in pdf_by_id:
            pdf_by_id[key] = href

    seen_ids: set[str] = set()
    pairs: list[tuple[str, str | None]] = []

    # Pass 2: walk the document in DOM order. ``find_all`` yields parents
    # before children, so the outermost containing element produces every
    # arrest_id in source order via concatenated text — child re-encounters
    # are deduped via ``seen_ids``.
    for tag in soup.find_all(["a", "span", "div", "li", "td", "p", "article"]):
        text = tag.get_text(strip=True)
        for arrest_id in _ARREST_ID_PATTERN.findall(text):
            key = arrest_id.upper()
            if key not in seen_ids:
                seen_ids.add(key)
                pairs.append((arrest_id, pdf_by_id.get(key)))
        if tag.name == "a":
            href_raw = tag.get("href", "")
            href = str(href_raw) if href_raw else ""
            for arrest_id in _ARREST_ID_PATTERN.findall(href):
                key = arrest_id.upper()
                if key not in seen_ids:
                    seen_ids.add(key)
                    pairs.append((arrest_id, pdf_by_id.get(key)))

    if pairs:
        return pairs

    # No arrest IDs anywhere. Distinguish "legitimate empty page" (end of
    # pagination) from "schema drift" (site redesigned, wrapper gone): if a
    # known listing wrapper is present we trust Drupal that this page is
    # simply empty; otherwise we surface drift so the circuit breaker fires.
    def _is_listing_wrapper(css_class: str | list[str] | None) -> bool:
        if css_class is None:
            return False
        if isinstance(css_class, str):
            tokens = css_class.split()
        else:
            tokens = list(css_class)
        return "view-content" in tokens or any(
            "view-legislation-search" in t for t in tokens
        )

    if soup.find("div", class_=_is_listing_wrapper) is not None:
        return []
    raise SchemaDriftError(f"rvvb: no listing wrapper found at {url}")


def _parse_listing_page(soup: BeautifulSoup, url: str) -> list[str]:
    """
    Backward-compatible wrapper: returns just the arrest_id strings.

    Existing tests + callers that don't need the PDF URL keep working;
    new code paths use ``_parse_listing_with_pdfs`` directly.
    """
    return [arrest_id for arrest_id, _ in _parse_listing_with_pdfs(soup, url)]


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
        async for arrest_id, page_index, pdf_url in source.paginate(
            start_page=start_page,
            limit=limit,
            year_filter=years,
        ):
            current_page = page_index
            try:
                # Tier 2: download PDF (pdf_url is the absolute href harvested
                # from the listing — month-folder URLs vary, so we rely on
                # the parsed value rather than constructing a path).
                pdf_path = await source.download_pdf(
                    arrest_id, pdf_url=pdf_url, dest_dir=dest_dir
                )

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
