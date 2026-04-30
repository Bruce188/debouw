"""
Brussels Capital Region scraper — openpermits.brussels (Track B: bs4 + httpx).

Reads permit dossiers from the public OpenPermits portal (COBAT regime).
Static HTML pages served by nginx; no JavaScript rendering required.

Track selection: bs4+httpx (chosen in Phase 1 spike — openpermits.brussels
serves pre-rendered HTML; mybrugis.brussels is DNS-unreachable).

ToS / GDPR:
- robots.txt allows /fr/* and /nl/* pages (Disallow: /api/*).
- Throttle: 2.0 s/req (spike recommendation, stored in throttle_brussels_seconds).
- applicant_name=None: GDPR posture per analysis — name not displayed on portal.
- Identified User-Agent: nominatim_user_agent setting.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import structlog
from bs4 import BeautifulSoup

from debouw.ingest.sources.base import SchemaDriftError, Source
from debouw.ingest.url_safety import is_brussels_attachment_allowed
from debouw.models.permit import (
    Address,
    PermitProject,
    PermitProjectStatus,
    PublicInquiry,
)

log = structlog.get_logger(__name__)

# Brussels permit reference: NN/TYPE/NUMBER
# NN  = 2-digit district code (01–19)
# TYPE = 2-10 uppercase alphanumeric chars + underscores (e.g. PU, PFD, GOU_PU, PE)
# NUMBER = 7-digit sequence
_BRU_REF_PATTERN = re.compile(r"^([0-1][0-9])/([A-Z][A-Z0-9_]{1,9})/([0-9]{7})$")

# Belgian address pattern: "<street + house>  NNNN  <Municipality>"
# Example: "Chaussée de Waterloo 1142 1180 Uccle" → street_full="Chaussée de
# Waterloo 1142", postcode="1180", municipality="Uccle". Lazy ``street`` lets
# the postcode anchor on the rightmost 4-digit token followed by an alphabetic
# municipality, which correctly skips numeric house numbers like "1142".
_BE_ADDRESS_PATTERN = re.compile(
    r"^(?P<street>.+?)\s+(?P<postcode>\d{4})\s+"
    r"(?P<municipality>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-']+?)\s*$"
)


def _parse_be_address(raw: str) -> dict[str, str | None]:
    """
    Parse a flattened Belgian address ("<street + house> NNNN <Municipality>")
    into structured fields. Returns dict with street/postcode/municipality keys
    populated when the regex matches; values default to ``None``.

    The municipality + postcode are required by the dashboard's gemeente filter
    and by Geopunt overlays — without them the row's ``Address(raw=...)``
    payload yields an unfilterable, geoless project. Splitting in the ingester
    keeps the parser deterministic and unit-testable.
    """
    out: dict[str, str | None] = {
        "street": None,
        "postcode": None,
        "municipality": None,
    }
    if not raw:
        return out
    m = _BE_ADDRESS_PATTERN.match(raw)
    if m:
        # Strip trailing punctuation from the street capture — Belgian
        # addresses occasionally use a comma between house number and
        # postcode ("Korenmarkt 1, 9000 Gent") and the lazy regex picks
        # the comma up as part of the street.
        out["street"] = m["street"].rstrip(",.;").strip()
        out["postcode"] = m["postcode"]
        out["municipality"] = m["municipality"]
    return out

# How far back we page when limit is not set — current + previous year-months
_DEFAULT_MONTHS_BACK = 1

# Listing path: /fr/event/submission/{year}/{month}
_LISTING_PATH = "/fr/event/submission/{year}/{month}"
# Detail path: /fr/_{ref_no_slash} → slash-escaped as /fr/_NN/TYPE/NUMBER
_DETAIL_PATH = "/fr/_{ref}"


def _parse_date(value: str | None) -> date | None:
    """Parse ISO-8601 datetime string to date (accepts 'YYYY-MM-DD HH:MM:SS' format)."""
    if not value:
        return None
    try:
        # Try full datetime first
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _status_from_case_status(case_status: str | None) -> PermitProjectStatus:
    """Map openpermits case_status string to PermitProjectStatus enum."""
    mapping = {
        "instruction": PermitProjectStatus.IN_PUBLIC_INQUIRY,
        "referral": PermitProjectStatus.IN_PUBLIC_INQUIRY,
        "accepted": PermitProjectStatus.DECIDED,
        "refused": PermitProjectStatus.DECIDED,
        "abandoned": PermitProjectStatus.CLOSED,
    }
    return mapping.get(case_status or "", PermitProjectStatus.INTAKE)


class BrusselsSource(Source):
    """Scraper for openpermits.brussels (CoBAT permit portal)."""

    source_key: ClassVar[str] = "brussels"

    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield Brussels permit references (NN/TYPE/NUMBER) from listing pages.

        Scrapes the current month's submission page. Links have href
        ``/fr/_NN/TYPE/NUMBER`` — extracts ``NN/TYPE/NUMBER``.
        """
        now = datetime.now(timezone.utc)
        path = _LISTING_PATH.format(year=now.year, month=now.month)
        response = await self._client.get(path)
        soup = BeautifulSoup(response.text, "html.parser")

        count = 0
        for anchor in soup.find_all("a", href=True):
            href: str = anchor["href"]
            # href pattern: /fr/_NN/TYPE/NUMBER  (leading underscore then slash-encoded ref)
            if not (href.startswith("/fr/_") or href.startswith("/nl/_")):
                continue
            # Strip language prefix and underscore: /fr/_NN/TYPE/NUMBER → NN/TYPE/NUMBER
            ref = href.split("_", 1)[-1]  # "NN/TYPE/NUMBER"
            if not _BRU_REF_PATTERN.match(ref):
                continue
            yield ref
            count += 1
            if limit is not None and count >= limit:
                return

    async def detail_pass(
        self, ref: str
    ) -> tuple[PermitProject, PublicInquiry | None]:
        """Fetch and parse a single dossier detail page.

        ``ref`` must match _BRU_REF_PATTERN (e.g. ``01/PU/1984289``).
        The detail URL is ``/fr/_01/PU/1984289``.
        """
        if not _BRU_REF_PATTERN.match(ref):
            raise SchemaDriftError(
                f"brussels: reference '{ref}' does not match _BRU_REF_PATTERN"
            )

        path = _DETAIL_PATH.format(ref=ref)
        response = await self._client.get(path)
        html = response.text
        content_hash = hashlib.sha256(html.encode()).hexdigest()

        soup = BeautifulSoup(html, "html.parser")

        # --- Extract inline JSON (tabledatahistory) ---
        # The page embeds let tabledatahistory = [...]; with all case revisions.
        # The first entry is the most recent.
        history_data: list[dict] = []
        json_match = re.search(
            r"let tabledatahistory\s*=\s*(\[.*?\]);",
            html,
            re.DOTALL,
        )
        if json_match:
            try:
                history_data = json.loads(json_match.group(1))
            except json.JSONDecodeError as exc:
                log.warning(
                    "brussels_history_json_parse_failed",
                    ref=ref,
                    error=str(exc),
                )

        if not history_data:
            raise SchemaDriftError(
                f"brussels: tabledatahistory JSON not found or empty at {path}"
            )

        # Most recent revision is at index 0 in the history array
        latest = history_data[0]

        # --- Address from h1.card-title ---
        h1_el = soup.find("h1", class_="card-title")
        if h1_el is None:
            raise SchemaDriftError(
                f"brussels: <h1 class=card-title> not found at {path}"
            )
        # h1 content: "Address<br>Zipcode Municipality" — flatten to string
        address_raw = h1_el.get_text(separator=" ", strip=True)
        _addr_parsed = _parse_be_address(address_raw)

        # --- Title / description ---
        # Use the description-case paragraph for project description
        desc_el = soup.find(id="description-case")
        description: str | None = (
            desc_el.get_text(strip=True) if desc_el else None
        ) or None

        # Title: "ADDRESS — TYPE" composed from address + caseType
        case_type = latest.get("caseType") or latest.get("caseSubType") or "Permis"
        title = f"{address_raw} — {case_type}"

        # --- Status + inquiry ---
        case_status = latest.get("caseStatus")
        status = _status_from_case_status(case_status)

        inquiry_start = _parse_date(latest.get("inquiryStartDate"))
        inquiry_end = _parse_date(latest.get("inquiryEndDate"))
        inquiry: PublicInquiry | None = None
        if inquiry_start and inquiry_end:
            days_remaining = (inquiry_end - date.today()).days
            inquiry = PublicInquiry(
                external_id=ref,
                period_start=inquiry_start,
                period_end=inquiry_end,
                objection_deadline=inquiry_end,
                days_remaining=days_remaining if days_remaining >= 0 else None,
            )
            status = PermitProjectStatus.IN_PUBLIC_INQUIRY

        # --- trees_to_fell from JSON data ---
        trees_to_fell: int | None = None
        cut_trees = latest.get("cutTreesNumber")
        if cut_trees is not None:
            try:
                trees_to_fell = int(cut_trees)
            except (TypeError, ValueError):
                pass

        # --- Save raw HTML ---
        safe_ref = ref.replace("/", "_")
        raw_dir = (
            self._settings.data_root / "raw" / "brussels" / safe_ref
        )
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_html_path = raw_dir / "detail.html"
        raw_html_path.write_text(html, encoding="utf-8")

        # --- Attachment links (SSRF-gated) ---
        base = self._settings.openpermits_brussels_base
        parsed_base = urlparse(base)
        base_host = f"{parsed_base.scheme}://{parsed_base.netloc}"

        attachments: list[str] = []
        dossier_pdfs: list[Path] = []

        pdf_dir = raw_dir / "pdfs"

        for anchor in soup.find_all("a", href=True):
            href: str = anchor["href"]
            # Only collect links to downloadable documents
            if not any(
                kw in href
                for kw in ("/documents/", "/files/", "/download/", ".pdf")
            ):
                continue
            if href.startswith("http"):
                abs_url = href
            else:
                abs_url = base_host + "/" + href.lstrip("/")
            if not is_brussels_attachment_allowed(abs_url):
                log.warning(
                    "brussels_attachment_ssrf_rejected",
                    ref=ref,
                    url=abs_url,
                )
                continue
            attachments.append(abs_url)

        if attachments:
            pdf_dir.mkdir(parents=True, exist_ok=True)
            for abs_url in attachments:
                filename = abs_url.rstrip("/").split("/")[-1] or "attachment.pdf"
                local_path = pdf_dir / filename
                try:
                    pdf_response = await self._client.get(abs_url)
                    local_path.write_bytes(pdf_response.content)
                    dossier_pdfs.append(local_path)
                except Exception as exc:
                    log.warning(
                        "brussels_pdf_download_failed",
                        ref=ref,
                        url=abs_url,
                        error=str(exc),
                    )

        now_utc = datetime.now(timezone.utc)
        external_id = f"brussels:{ref}"
        detail_url = f"{base}/fr/_{ref}"

        project = PermitProject(
            external_id=external_id,
            source="brussels_openpermits",
            region="brussels",
            omv_reference=ref,
            detail_url=detail_url,
            title=title,
            description=description,
            applicant_name=None,  # GDPR: not displayed on portal
            address=Address(
                raw=address_raw,
                street=_addr_parsed["street"],
                postcode=_addr_parsed["postcode"],
                municipality=_addr_parsed["municipality"],
            ),
            project_type=case_type,
            status=status,
            trees_to_fell=trees_to_fell,
            attachments=attachments,
            dossier_pdfs=dossier_pdfs,
            overlays=None,  # pipeline fills this; Geopunt overlays VL-only — empty OK
            raw_html_path=raw_html_path,
            first_seen_at=now_utc,
            last_changed_at=now_utc,
            content_hash=content_hash,
            decision_regime="post_2026_reform",
        )
        return project, inquiry
