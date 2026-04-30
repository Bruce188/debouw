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
from typing import Any, ClassVar
from urllib.parse import urlparse

import structlog
from bs4 import BeautifulSoup

from debouw.ingest.sources.base import SchemaDriftError, Source
from debouw.ingest.url_safety import is_brussels_attachment_allowed
from debouw.models.permit import (
    Address,
    GeoPoint,
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

# Regexes for description parsing: units, floors, iioa_class (FR + NL-Brussels)
_DESC_FLOORS_FR_RE = re.compile(
    r"(\d{1,2})\s*(?:étages?|niveaux|R\+(\d+))",
    re.IGNORECASE | re.MULTILINE,
)
_DESC_FLOORS_NL_RE = re.compile(
    r"(\d{1,2})\s*(?:verdiepingen|bouwlagen)",
    re.IGNORECASE | re.MULTILINE,
)
_DESC_UNITS_FR_RE = re.compile(
    r"(\d{1,3})\s*(?:logements?|appartements?|unités?\s+de\s+logement)",
    re.IGNORECASE | re.MULTILINE,
)
_DESC_UNITS_NL_RE = re.compile(
    r"(\d{1,3})\s*(?:woningen|appartementen|wooneenheden)",
    re.IGNORECASE | re.MULTILINE,
)
_DESC_IIOA_FR_RE = re.compile(
    r"classe\s+(I{1,3}|[1-3])\b",
    re.IGNORECASE | re.MULTILINE,
)
_DESC_IIOA_NL_RE = re.compile(
    r"klasse\s+(I{1,3}|[1-3])\b",
    re.IGNORECASE | re.MULTILINE,
)
_ROMAN_TO_INT = {"I": 1, "II": 2, "III": 3}

# Module-scope cached Lambert-72 → WGS84 transformer (EPSG:31370 → EPSG:4326)
# Lazy-initialized to avoid import cost when pyproj is unavailable.
_LAMBERT72_TRANSFORMER = None


def _get_lambert_transformer():
    """Return a lazily initialized pyproj Transformer (Lambert-72 → WGS84)."""
    global _LAMBERT72_TRANSFORMER
    if _LAMBERT72_TRANSFORMER is None:
        try:
            import pyproj
            _LAMBERT72_TRANSFORMER = pyproj.Transformer.from_crs(
                "EPSG:31370", "EPSG:4326", always_xy=True
            )
        except Exception:
            return None
    return _LAMBERT72_TRANSFORMER


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


def _lambert72_centroid_to_wgs84(geometry: dict | None) -> GeoPoint | None:
    """
    Compute the centroid of a Lambert-72 polygon (GeoJSON-style) and convert
    it to WGS84 (EPSG:4326).

    ``geometry`` is expected to have shape ``{"coordinates": [[[x, y], ...]]}``.
    The outer ring (coordinates[0]) is used; a naive mean centroid is computed.

    Returns a GeoPoint, or None when geometry is absent, malformed, or produces
    coords outside the Belgium bbox (the GeoPoint validator rejects those).
    """
    if geometry is None:
        return None
    transformer = _get_lambert_transformer()
    if transformer is None:
        return None
    try:
        import pyproj  # noqa: F401  (ensure available)
        from pydantic import ValidationError as PydanticValidationError

        ring = geometry["coordinates"][0]
        if not ring:
            return None
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        lon, lat = transformer.transform(mean_x, mean_y)
        return GeoPoint(lat=lat, lon=lon)
    except Exception as exc:
        log.warning("brussels_lambert_to_wgs84_failed", error=str(exc))
        return None


def _parse_description_for_units_floors_iioa(desc: str, lang: str) -> dict[str, int | None]:
    """
    Apply FR/NL regex set to description text, returning units/floors/iioa_class.
    First non-null match per key wins.
    """
    floors: int | None = None
    units: int | None = None
    iioa_class: int | None = None

    floor_re = _DESC_FLOORS_FR_RE if lang == "fr" else _DESC_FLOORS_NL_RE
    units_re = _DESC_UNITS_FR_RE if lang == "fr" else _DESC_UNITS_NL_RE
    iioa_re = _DESC_IIOA_FR_RE if lang == "fr" else _DESC_IIOA_NL_RE

    m = floor_re.search(desc)
    if m:
        try:
            floors = int(m.group(1))
        except (ValueError, TypeError):
            pass

    m = units_re.search(desc)
    if m:
        try:
            units = int(m.group(1))
        except (ValueError, TypeError):
            pass

    m = iioa_re.search(desc)
    if m:
        raw = m.group(1).upper()
        iioa_class = _ROMAN_TO_INT.get(raw) or (int(raw) if raw.isdigit() else None)

    return {"units": units, "floors": floors, "iioa_class": iioa_class}


def _sum_authorized_floor_area(floor_area: dict | None) -> float | None:
    """
    Sum ``typology["authorized"]`` values across all typologies in the floorArea dict.
    Treats missing, non-numeric, or negative values as 0 contribution.
    Returns None when floor_area is None or empty, otherwise the sum (may be 0.0).
    """
    if not floor_area:
        return None
    total = 0.0
    found_any = False
    for typology_data in floor_area.values():
        if not isinstance(typology_data, dict):
            continue
        found_any = True
        try:
            val = float(typology_data.get("authorized", 0) or 0)
        except (TypeError, ValueError):
            val = 0.0
        total += max(0.0, val)
    return total if found_any else None


def _parse_brussels_html(html: str, ref: str) -> dict[str, Any]:
    """
    Parse Brussels openpermits detail page HTML and return a dict of extracted fields.

    This free function is shared by BrusselsSource.detail_pass and the
    reparse-brussels CLI command (Task 3.3) so both consumers share the same
    parsing logic.

    Keys returned:
      address_raw, address_parsed, description, case_type, case_status,
      inquiry_start, inquiry_end, trees_to_fell, error_weight, floor_area_m2,
      mer_status, case_language, geometry, history_data, latest
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Extract inline JSON (tabledatahistory) ---
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
            f"brussels: tabledatahistory JSON not found or empty for ref={ref}"
        )

    # Most recent revision is at index 0
    latest = history_data[0]

    # --- Address from h1.card-title ---
    h1_el = soup.find("h1", class_="card-title")
    if h1_el is None:
        raise SchemaDriftError(
            f"brussels: <h1 class=card-title> not found for ref={ref}"
        )
    address_raw = h1_el.get_text(separator=" ", strip=True)
    address_parsed = _parse_be_address(address_raw)

    # --- Title / description ---
    desc_el = soup.find(id="description-case")
    description: str | None = (
        desc_el.get_text(strip=True) if desc_el else None
    ) or None

    # Concatenate scope hints so heritage/ongunstig regex sees them
    scopes = latest.get("scopes", []) or []
    if isinstance(scopes, list) and scopes:
        scope_text = " ".join(str(s) for s in scopes)
        description = (description + " " + scope_text).strip() if description else scope_text

    # --- Status ---
    case_status = latest.get("caseStatus")
    case_type = latest.get("caseType") or latest.get("caseSubType") or "Permis"

    # --- Inquiry dates ---
    inquiry_start = _parse_date(latest.get("inquiryStartDate"))
    inquiry_end = _parse_date(latest.get("inquiryEndDate"))

    # --- trees_to_fell ---
    trees_to_fell: int | None = None
    cut_trees = latest.get("cutTreesNumber")
    if cut_trees is not None:
        try:
            trees_to_fell = int(cut_trees)
        except (TypeError, ValueError):
            pass

    # --- error_weight ---
    error_weight: float | None = None
    try:
        error_weight = float(latest["errorWeight"])
    except (KeyError, TypeError, ValueError):
        pass

    # --- floor_area_m2 ---
    floor_area_m2 = _sum_authorized_floor_area(latest.get("floorArea"))

    # --- mer_status ---
    has_impact_study = bool(latest.get("hasImpactStudy"))
    has_impact_report = bool(latest.get("hasImpactReport"))
    if has_impact_study:
        mer_status = "mer_plicht"
    elif has_impact_report:
        mer_status = "screening"
    else:
        mer_status = None

    # --- case_language ---
    # Normalize to lowercase; Brussels portal sends "FR" or "NL" in uppercase
    raw_lang = latest.get("caseLanguage") or "fr"
    case_language = raw_lang.lower() if isinstance(raw_lang, str) else "fr"
    # Clamp to allowed values: "fr" or "nl"
    if case_language not in ("fr", "nl"):
        case_language = "fr"

    # --- geometry (Lambert-72) ---
    # Geometry may be a dict or a JSON-encoded string (as seen in openpermits.brussels)
    raw_geom = latest.get("geometry") or latest.get("location")
    geometry = None
    if isinstance(raw_geom, dict):
        geometry = raw_geom
    elif isinstance(raw_geom, str):
        try:
            geometry = json.loads(raw_geom)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "address_raw": address_raw,
        "address_parsed": address_parsed,
        "description": description,
        "case_type": case_type,
        "case_status": case_status,
        "inquiry_start": inquiry_start,
        "inquiry_end": inquiry_end,
        "trees_to_fell": trees_to_fell,
        "error_weight": error_weight,
        "floor_area_m2": floor_area_m2,
        "mer_status": mer_status,
        "case_language": case_language,
        "geometry": geometry,
        "history_data": history_data,
        "latest": latest,
    }


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

        # Parse the HTML using the shared free function
        parsed = _parse_brussels_html(html, ref)

        address_raw = parsed["address_raw"]
        address_parsed = parsed["address_parsed"]
        description = parsed["description"]
        case_type = parsed["case_type"]
        case_status = parsed["case_status"]
        inquiry_start = parsed["inquiry_start"]
        inquiry_end = parsed["inquiry_end"]
        trees_to_fell = parsed["trees_to_fell"]
        error_weight = parsed["error_weight"]
        floor_area_m2 = parsed["floor_area_m2"]
        mer_status = parsed["mer_status"]
        case_language = parsed["case_language"]
        geometry = parsed["geometry"]

        # Title: "ADDRESS — TYPE"
        title = f"{address_raw} — {case_type}"

        # Status + inquiry
        status = _status_from_case_status(case_status)
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

        soup = BeautifulSoup(html, "html.parser")
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

        # Lambert-72 → WGS84 geometry conversion
        geo_point = _lambert72_centroid_to_wgs84(geometry)

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
                street=address_parsed["street"],
                postcode=address_parsed["postcode"],
                municipality=address_parsed["municipality"],
                point=geo_point,
            ),
            project_type=case_type,
            status=status,
            trees_to_fell=trees_to_fell,
            mer_status=mer_status,
            case_language=case_language,
            error_weight=error_weight,
            floor_area_m2=floor_area_m2,
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
