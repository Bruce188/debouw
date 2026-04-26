"""
Gent scraper — reads active public inquiry dossiers from consultatieomgeving.net.

Uses BeautifulSoup with html.parser (lxml not available on Python 3.14).
All HTTP calls go through the ThrottledHttpClient from create_http_client().
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar

import httpx
import structlog
from bs4 import BeautifulSoup

from debouw.ingest.sources.base import SchemaDriftError, Source
from debouw.models.permit import (
    Address,
    PermitProject,
    PermitProjectStatus,
    PublicInquiry,
)

log = structlog.get_logger(__name__)

_INDEX_PATH = "/nl/OpenbareOnderzoeken"
_DETAIL_PATH = "/nl/OpenbareOnderzoeken/Details/{uuid}"


def _parse_date_attr(value: str | None) -> date | None:
    """Parse a data-timestamp attribute (epoch seconds) to a date."""
    if not value:
        return None
    try:
        ts = int(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except (ValueError, OSError):
        return None


class GentSource(Source):
    """Scraper for the Gent omgevingsvergunning consultation portal."""

    source_key: ClassVar[str] = "gent"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        # ASP.NET emits NSC_* cookies; persistent jar prevents 403s on Bestand
        self._cookies = httpx.Cookies()

    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield UUIDs of active public inquiry dossiers."""
        response = await self._client.get(_INDEX_PATH)
        soup = BeautifulSoup(response.text, "html.parser")

        ul = soup.find("ul", class_="list")
        if ul is None:
            raise SchemaDriftError(
                f"gent: <ul class=list> not found at {_INDEX_PATH}"
            )

        count = 0
        for li in ul.find_all("li"):
            title_el = li.find(class_="remTitel")
            if title_el is None:
                continue
            anchor = title_el.find("a", href=True)
            if anchor is None:
                continue
            href = anchor["href"]
            # href like "/burger/nl/OpenbareOnderzoeken/Details/<uuid>"
            parts = href.rstrip("/").split("/")
            uuid = parts[-1] if parts else None
            if not uuid:
                continue
            yield uuid
            count += 1
            if limit is not None and count >= limit:
                return

    async def detail_pass(
        self, uuid: str
    ) -> tuple[PermitProject, PublicInquiry | None]:
        """Fetch and parse a single dossier detail page."""
        path = _DETAIL_PATH.format(uuid=uuid)
        response = await self._client.get(path, cookies=self._cookies)
        html = response.text
        content_hash = hashlib.sha256(html.encode()).hexdigest()

        soup = BeautifulSoup(html, "html.parser")

        # --- Parse labelled dt/dd blocks ---
        def _find_dd(label: str) -> str | None:
            dt = soup.find("dt", string=lambda s: s and label in s)
            if dt is None:
                return None
            dd = dt.find_next_sibling("dd")
            return dd.get_text(strip=True) if dd else None

        omv_reference = _find_dd("Projectnummer")
        if omv_reference is None:
            raise SchemaDriftError(
                f"gent: Projectnummer dt not found at {path}"
            )

        authority = _find_dd("Vergunningverlenende overheid")
        if authority is None:
            raise SchemaDriftError(
                f"gent: Vergunningverlenende overheid dt not found at {path}"
            )

        project_type = _find_dd("Aard van de aanvraag")
        if project_type is None:
            raise SchemaDriftError(
                f"gent: Aard van de aanvraag dt not found at {path}"
            )

        title_text = _find_dd("Onderwerp")
        if title_text is None:
            raise SchemaDriftError(
                f"gent: Onderwerp dt not found at {path}"
            )

        locatie_text = _find_dd("Locatie(s)") or ""
        # Strip degenerate "," placeholder
        if locatie_text.strip() == ",":
            locatie_text = ""

        # --- Save raw HTML ---
        raw_dir = (
            self._settings.data_root / "raw" / "gent_consultatie" / uuid
        )
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_html_path = raw_dir / "detail.html"
        raw_html_path.write_text(html, encoding="utf-8")

        # --- PDF attachments ---
        pdf_dir = raw_dir / "pdfs"
        attachments: list[str] = []
        dossier_pdfs: list[Path] = []

        base_url = self._settings.gent_consultatie_base
        # Site root = everything before the first path segment of base_url
        # e.g. "https://gent.consultatieomgeving.net/burger" → "https://gent.consultatieomgeving.net"
        from urllib.parse import urlparse
        _parsed = urlparse(base_url)
        site_root = f"{_parsed.scheme}://{_parsed.netloc}"

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "OpenbareOnderzoeken/Bestand?id=" in href:
                # Build absolute URL relative to site root (href may start with /burger/...)
                if href.startswith("http"):
                    abs_url = href
                else:
                    abs_url = site_root + "/" + href.lstrip("/")
                attachments.append(abs_url)

        if attachments:
            pdf_dir.mkdir(parents=True, exist_ok=True)
            for abs_url in attachments:
                bestand_id = abs_url.split("id=")[-1].split("&")[0]
                local_path = pdf_dir / f"{bestand_id}.pdf"
                try:
                    pdf_response = await self._client.get(
                        abs_url, cookies=self._cookies
                    )
                    local_path.write_bytes(pdf_response.content)
                    dossier_pdfs.append(local_path)
                except Exception as exc:
                    log.warning(
                        "gent_pdf_download_failed",
                        url=abs_url,
                        error=str(exc),
                    )

        # --- Description: Onderwerp → first PDF text → None ---
        description: str | None = title_text if title_text else None
        if not description and dossier_pdfs:
            try:
                from debouw.ingest.pdf import extract_text
                pdf_text = await extract_text(dossier_pdfs[0])
                description = pdf_text[:500] if pdf_text else None
            except NotImplementedError:
                description = None

        # --- Public inquiry period from index cards (re-parse from detail) ---
        inquiry: PublicInquiry | None = None
        datum_van_el = soup.find(class_="remOoDatumVan")
        datum_tot_el = soup.find(class_="remOoDatumTot")
        if datum_van_el is not None and datum_tot_el is not None:
            period_start = _parse_date_attr(datum_van_el.get("data-timestamp"))
            period_end = _parse_date_attr(datum_tot_el.get("data-timestamp"))
            if period_start and period_end:
                days_remaining = (period_end - date.today()).days
                inquiry = PublicInquiry(
                    external_id=uuid,
                    period_start=period_start,
                    period_end=period_end,
                    objection_deadline=period_end,
                    days_remaining=days_remaining if days_remaining >= 0 else None,
                )
                status = PermitProjectStatus.IN_PUBLIC_INQUIRY
            else:
                status = PermitProjectStatus.INTAKE
        else:
            status = PermitProjectStatus.INTAKE

        now = datetime.now(timezone.utc)
        external_id = f"gent:{omv_reference}"
        detail_url = f"{site_root}/burger/nl/OpenbareOnderzoeken/Details/{uuid}"

        project = PermitProject(
            external_id=external_id,
            source="gent_consultatie",
            omv_reference=omv_reference,
            detail_url=detail_url,
            title=title_text or omv_reference,
            description=description,
            applicant_name=None,  # GDPR: Gent does not display applicant name
            address=Address(raw=locatie_text),
            project_type=project_type,
            status=status,
            attachments=attachments,
            dossier_pdfs=dossier_pdfs,
            overlays=None,  # pipeline fills this
            raw_html_path=raw_html_path,
            first_seen_at=now,
            last_changed_at=now,
            content_hash=content_hash,
            decision_regime="post_2026_reform",
        )
        return project, inquiry
