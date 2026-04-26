"""Inzageloket scraper — Playwright headed-browser for Anubis-protected pages.

Source: https://omgevingsloketinzage.omgeving.vlaanderen.be
Throttle: 5 s/page-goto (Settings.throttle_inzageloket_seconds).
UA: identified per Phase 4 Task 1.2 fix (http.py identified_sources).

Anubis posture (analysis-v5 § Architecture deltas a):
- Single async Playwright instance per ingest run.
- chromium.launch(headless=False) — fresh BrowserContext per dossier.
- After page.goto(url): wait_for_load_state("networkidle", timeout=30_000).

Cross-source normalization:
- source = "vlaanderen_inzage"
- external_id = f"vlaanderen_inzage:{omv_reference}"
- detail_url = canonical permalink (no query strings)
- decision_regime = "post_2026_reform" (all Phase 4 dossiers post-cutover)
- applicant_name = None (GDPR — Inzageloket may or may not display)

Attachment download:
- PDF URLs scraped from detail page.
- is_inzageloket_attachment_allowed() check BEFORE handoff to ThrottledHttpClient.
- Local path: data/raw/vlaanderen_inzage/<external_id>/pdfs/<sha8>.pdf.

Schema-drift detection:
- Listing: missing results-container → SchemaDriftError.
- Detail: missing Projectnummer / Onderwerp → SchemaDriftError.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from debouw.config import Settings
from debouw.ingest.pdf import _MAX_PDF_SIZE_BYTES, extract_text
from debouw.ingest.sources.base import SchemaDriftError, Source
from debouw.ingest.url_safety import is_inzageloket_attachment_allowed
from debouw.models.permit import (
    Address,
    PermitProject,
    PermitProjectStatus,
    PublicInquiry,
)

log = structlog.get_logger(__name__)

_LISTING_PATH = "/zoeken"
_DETAIL_PATH_PREFIX = "/dossier/"
_NETWORK_IDLE_TIMEOUT_MS = 30_000

# B1 — path traversal defence: OMV reference must match strict pattern before
# building any file-system path derived from it.
_OMV_REF_PATTERN = re.compile(r"^OMV[_A-Z0-9.\-]{4,64}$")

# Selectors — confirmed against fixture HTML
_LISTING_CONTAINER_CLASS = "dossier-results"
_LISTING_CARD_SELECTOR = "a.dossier-card"
_DETAIL_FIELD_LABEL = "dt"  # definition list terms
_DETAIL_FIELD_VALUE = "dd"  # definition list values


def _safe_text(el) -> str | None:
    """Return stripped text from a BS4 element, or None if el is None."""
    if el is None:
        return None
    text = el.get_text(strip=True)
    return text if text else None


def _parse_dutch_date(value: str | None) -> date | None:
    """Parse a date in 'DD/MM/YYYY' or 'YYYY-MM-DD' format."""
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_dl_value(soup: BeautifulSoup, label_text: str) -> str | None:
    """Find a <dt> containing label_text and return the text of the next <dd>."""
    for dt in soup.find_all("dt"):
        if label_text.lower() in dt.get_text(strip=True).lower():
            dd = dt.find_next_sibling("dd")
            return _safe_text(dd)
    return None


def _sha8(url: str) -> str:
    """8-char hex prefix of SHA-256 of a URL — used as PDF filename."""
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def _parse_detail_page(
    html: str,
    base_url: str,
    external_id: str,
    raw_html_path: Path,
    data_dir: Path,
) -> tuple[dict, str | None, str | None, str | None, list[str]]:
    """Parse a detail page HTML.

    Returns:
        (fields_dict, omv_reference, municipality, description, pdf_urls)
    """
    soup = BeautifulSoup(html, "html.parser")

    # Required: Projectnummer
    omv_reference = _extract_dl_value(soup, "Projectnummer")
    if not omv_reference:
        raise SchemaDriftError(
            f"inzageloket: Projectnummer not found for {external_id}"
        )

    # Required: Onderwerp (title/description)
    onderwerp = _extract_dl_value(soup, "Onderwerp")
    if not onderwerp:
        raise SchemaDriftError(
            f"inzageloket: Onderwerp not found for {external_id}"
        )

    # Optional fields
    locatie = _extract_dl_value(soup, "Locatie") or _extract_dl_value(soup, "Straat") or ""
    gemeente = _extract_dl_value(soup, "Gemeente")
    aard = _extract_dl_value(soup, "Aard van de aanvraag")

    # Public inquiry dates — best effort
    inquiry_start = _parse_dutch_date(_extract_dl_value(soup, "Start openbaar onderzoek"))
    inquiry_end = _parse_dutch_date(_extract_dl_value(soup, "Einde openbaar onderzoek"))

    # PDF attachment URLs — SSRF filtered
    raw_urls: list[str] = []
    for a in soup.find_all("a", href=True):
        # bs4 may return AttributeValueList for multi-valued attrs; coerce to str.
        href_raw = a.get("href")
        if href_raw is None:
            continue
        href: str = href_raw if isinstance(href_raw, str) else " ".join(href_raw)
        if not href:
            continue
        if href.endswith(".pdf") or "pdf" in href.lower():
            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            raw_urls.append(abs_url)

    allowed_pdf_urls = [u for u in raw_urls if is_inzageloket_attachment_allowed(u)]

    fields = {
        "onderwerp": onderwerp,
        "locatie": locatie,
        "gemeente": gemeente,
        "aard": aard,
        "inquiry_start": inquiry_start,
        "inquiry_end": inquiry_end,
    }
    return fields, omv_reference, gemeente, onderwerp, allowed_pdf_urls


class InzageloketSource(Source):
    """Scraper for Vlaanderen-wide omgevingsvergunning dossiers via Inzageloket."""

    source_key: ClassVar[str] = "inzageloket"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._settings = settings
        self._playwright = None
        self._browser = None
        self._goto_lock = asyncio.Lock()
        self._last_goto_at: float | None = None

    async def __aenter__(self) -> "InzageloketSource":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=False)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        await super().__aexit__(*exc)

    async def _throttled_goto(self, page, url: str) -> None:
        """Navigate to url with polite throttle (Settings.throttle_inzageloket_seconds)."""
        async with self._goto_lock:
            now = time.monotonic()
            if self._last_goto_at is not None:
                elapsed = now - self._last_goto_at
                wait = self._settings.throttle_inzageloket_seconds - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
            await page.goto(
                url,
                wait_until="networkidle",
                timeout=_NETWORK_IDLE_TIMEOUT_MS,
            )
            self._last_goto_at = time.monotonic()

    async def _get_page_html(self, url: str) -> str:
        """Open a fresh BrowserContext, navigate to url, return rendered HTML."""
        if self._browser is None:
            raise RuntimeError(
                "InzageloketSource must be used as async context manager "
                "(await async with InzageloketSource(settings) as src:)"
            )
        context = await self._browser.new_context()
        try:
            page = await context.new_page()
            await self._throttled_goto(page, url)
            html: str = await page.content()
            return html
        finally:
            await context.close()

    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield OMV reference strings from Inzageloket listing pages."""
        base = self._settings.inzageloket_base
        listing_url = f"{base}{_LISTING_PATH}"
        html = await self._get_page_html(listing_url)
        soup = BeautifulSoup(html, "html.parser")

        container = soup.find(class_=_LISTING_CONTAINER_CLASS)
        if container is None:
            raise SchemaDriftError(
                f"inzageloket: .{_LISTING_CONTAINER_CLASS} not found at {listing_url}"
            )

        count = 0
        for a in container.find_all("a", href=True):
            # bs4 may return AttributeValueList for multi-valued attrs; coerce to str.
            href_raw = a.get("href")
            if href_raw is None:
                continue
            href: str = href_raw if isinstance(href_raw, str) else " ".join(href_raw)
            if href and _DETAIL_PATH_PREFIX in href:
                # Extract OMV reference from /dossier/<OMV_REF>
                parts = href.rstrip("/").split(_DETAIL_PATH_PREFIX)
                if len(parts) > 1 and parts[-1]:
                    omv_ref = parts[-1].split("?")[0]
                    # B1: validate before yielding — reject path traversal attempts
                    if not _OMV_REF_PATTERN.match(omv_ref):
                        log.warning(
                            "inzageloket_invalid_omv_ref",
                            omv_ref=omv_ref,
                            href=href,
                        )
                        continue
                    yield omv_ref
                    count += 1
                    if limit is not None and count >= limit:
                        return

    async def detail_pass(
        self,
        identifier: str,
    ) -> tuple[PermitProject, PublicInquiry | None]:
        """Fetch and parse one Inzageloket dossier page.

        identifier: OMV reference string (e.g. "OMV_2025123456")
        Raises SchemaDriftError if required fields are missing.
        Raises ValueError if identifier fails OMV reference validation (B1).
        """
        # B1: validate identifier before building any file-system path
        if not _OMV_REF_PATTERN.match(identifier):
            log.warning(
                "inzageloket_invalid_omv_ref",
                identifier=identifier,
            )
            raise ValueError(
                f"inzageloket: identifier {identifier!r} does not match OMV reference "
                f"pattern — possible path traversal attempt"
            )

        base = self._settings.inzageloket_base
        detail_url_str = f"{base}{_DETAIL_PATH_PREFIX}{identifier}"
        html = await self._get_page_html(detail_url_str)

        external_id = f"vlaanderen_inzage:{identifier}"
        now = datetime.now(tz=timezone.utc)
        content_hash = hashlib.sha256(html.encode()).hexdigest()

        # Persist raw HTML
        data_dir = Path("data/raw/vlaanderen_inzage") / external_id
        data_dir.mkdir(parents=True, exist_ok=True)
        raw_html_path = data_dir / "detail.html"
        raw_html_path.write_text(html, encoding="utf-8")

        fields, omv_ref, gemeente, onderwerp, allowed_pdf_urls = _parse_detail_page(
            html=html,
            base_url=base,
            external_id=external_id,
            raw_html_path=raw_html_path,
            data_dir=data_dir,
        )

        # Download allowed PDFs
        pdfs_dir = data_dir / "pdfs"
        pdfs_dir.mkdir(parents=True, exist_ok=True)
        local_pdf_paths: list[Path] = []
        for pdf_url in allowed_pdf_urls:
            local_path = pdfs_dir / f"{_sha8(pdf_url)}.pdf"
            if local_path.exists():
                local_pdf_paths.append(local_path)
                continue
            try:
                # B2: streaming GET with byte counter — abort + delete if oversized.
                # follow_redirects=False defends against redirect-to-evil-host bypass.
                async with self._client.stream(
                    "GET", pdf_url, follow_redirects=False
                ) as response:
                    response.raise_for_status()
                    # Post-stream SSRF re-validation (defense vs N1 redirect bypass)
                    actual_host = str(response.url.host)
                    if not is_inzageloket_attachment_allowed(str(response.url)):
                        log.warning(
                            "inzageloket_pdf_ssrf_post_redirect_rejected",
                            external_id=external_id,
                            url=pdf_url,
                            actual_host=actual_host,
                        )
                        continue
                    byte_count = 0
                    oversize = False
                    with local_path.open("wb") as fh:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            byte_count += len(chunk)
                            if byte_count > _MAX_PDF_SIZE_BYTES:
                                oversize = True
                                break
                            fh.write(chunk)
                if oversize:
                    local_path.unlink(missing_ok=True)
                    log.warning(
                        "inzageloket_pdf_too_large",
                        external_id=external_id,
                        url=pdf_url,
                        bytes_seen=byte_count,
                        cap_bytes=_MAX_PDF_SIZE_BYTES,
                    )
                    continue
                local_pdf_paths.append(local_path)
                log.info(
                    "inzageloket_pdf_downloaded",
                    external_id=external_id,
                    url=pdf_url,
                    path=str(local_path),
                )
            except Exception as exc:
                log.warning(
                    "inzageloket_pdf_download_failed",
                    external_id=external_id,
                    url=pdf_url,
                    error=str(exc),
                )

        # Description fallback
        description: str | None = fields["onderwerp"]
        if not description and local_pdf_paths:
            description = (await extract_text(local_pdf_paths[0]))[:500] or None

        # Status
        inquiry_start = fields["inquiry_start"]
        inquiry_end = fields["inquiry_end"]
        status = (
            PermitProjectStatus.IN_PUBLIC_INQUIRY
            if inquiry_start is not None
            else PermitProjectStatus.INTAKE
        )

        # Build PermitProject
        clean_url = detail_url_str.split("?", 1)[0]
        address = Address(
            raw=fields["locatie"] or identifier,
            municipality=gemeente,
        )

        project = PermitProject(
            external_id=external_id,
            source="vlaanderen_inzage",
            omv_reference=omv_ref or identifier,
            detail_url=clean_url,  # type: ignore[arg-type]
            title=fields["onderwerp"],
            description=description,
            applicant_name=None,
            address=address,
            project_type=fields["aard"],
            status=status,
            attachments=[],  # raw scraped URLs excluded — only SSRF-cleared local paths used
            dossier_pdfs=local_pdf_paths,
            raw_html_path=raw_html_path,
            first_seen_at=now,
            last_changed_at=now,
            content_hash=content_hash,
            decision_regime="post_2026_reform",
        )

        inquiry: PublicInquiry | None = None
        if inquiry_start is not None and inquiry_end is not None:
            inquiry = PublicInquiry(
                external_id=external_id,
                period_start=inquiry_start,
                period_end=inquiry_end,
                objection_deadline=inquiry_end,
            )

        return project, inquiry
