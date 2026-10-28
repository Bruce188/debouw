"""
Multi-tenant scraper for Remmicom's "Consultatie Module Burger" platform
(<slug>.consultatieomgeving.net) — covers ~110 Flemish municipalities.

Every tenant serves the identical platform (footer: © Remmicom v3.3.2.x) at
``https://<slug>.consultatieomgeving.net/burger/nl/OpenbareOnderzoeken``, but
detail-page markup drifts per municipality because the body is a Word-export:

  - Gent:      <dl><dt>Label</dt><dd>value</dd> (legacy) or label paragraphs.
  - Antwerpen: a Word table — <td>Label:</td><td>value</td> rows.
  - Hasselt:   free prose ("Omgevingsloket nummer : OMV_…", "gelegen te …,
               kadastraal gekend …").
  - Aalter:    prose variant ("Omgevingsvergunning: OMV_…", "… en gekend in
               het kadaster als …").

Field extraction therefore uses per-field label synonym lists across three
structural forms (dt/dd, table row, paragraph) plus Dutch prose fallbacks,
raising SchemaDriftError only when NO synonym (or fallback) resolves the
required field.

GDPR posture (identical to Gent): applicant_name is NEVER persisted, even
when a tenant displays it in prose. Identified User-Agent on every request.
robots.txt is re-checked per tenant at runtime; tenants that gain a disallow
are skipped and logged.

Uses BeautifulSoup with html.parser. All HTTP goes through the throttled
client from create_http_client() — 1 req/2 s globally across tenants.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import AsyncIterator, Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar
from urllib import robotparser
from urllib.parse import urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from debouw.ingest.http import create_http_client
from debouw.ingest.sources.base import DossierVanishedError, SchemaDriftError, Source
from debouw.models.permit import (
    Address,
    PermitProject,
    PermitProjectStatus,
    PublicInquiry,
)

log = structlog.get_logger(__name__)

_INDEX_PATH = "/nl/OpenbareOnderzoeken"
_DETAIL_PATH = "/nl/OpenbareOnderzoeken/Details/{uuid}"

# 2026 detail pages no longer carry remOoDatum* spans; the inquiry period is
# prose: "... openbaar onderzoek, dat loopt van 12 juni 2026 tot en met 11
# juli 2026 ...". Parse it from the page text as a fallback.
_NL_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5,
    "juni": 6, "juli": 7, "augustus": 8, "september": 9, "oktober": 10,
    "november": 11, "december": 12,
}
_PERIODE_RE = re.compile(
    r"openbaar onderzoek[^.]*?van\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})"
    r"\s+tot(?:\s+en\s+met)?\s+(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})",
    re.IGNORECASE,
)

# --- Locatie(s) address parsing --------------------------------------------
# Observed raw strings (Gent "Locatie(s)" + Antwerpen "Ligging van het
# project" + Hasselt/Aalter prose):
#   "Duddegemstraat 29, 9051 Gent (afdeling 26 sectie A nr. 233D)"
#   "Suzanne Tassierstraat , 9052 Gent en (afdeling 24 sectie B nrs. 627N), ..."
#   "Leeuwstraat 84-90 en Sint-Lievenslaan 23-33B, 9000 Gent, , en (afdeling ...)"
#   "Lange Nieuwstraat 64-68, Eikenstraat 2 te 2000 Antwerpen"
#   "Bekstraat zn"
# Multiple street segments are joined with "en" or "," (primary = first
# segment). Cadastral parcels appear in parentheses, with or without an
# "afdeling N" prefix; only the first one is normalized into parcel_id.

# First parenthesized cadastral reference; "afdeling N" prefix is optional
# and "nr."/"nrs." both occur. Parcel numbers may carry "/" and "_" suffixes
# (blank-exponent notation), e.g. "1063/2 _" or "645_".
_PARCEL_RE = re.compile(
    r"\(\s*(?:afdeling\s+(\d+)\s+)?sectie\s+([A-Za-z])\s+nrs?\.\s*([^)]*?)\s*\)",
    re.IGNORECASE,
)
# 4-digit postcode followed by the municipality (runs until ',', '(' or ')').
_POSTCODE_RE = re.compile(r"\b(\d{4})\s+([^,()]+)")
# Trailing house number on a street segment: "37", "84-90", "23-33B", "64-64A".
_HOUSE_NUMBER_RE = re.compile(r"^(.*?)\s+(\d+[A-Za-z]?(?:\s*-\s*\d+[A-Za-z]?)?)$")


def parse_gent_address(raw: str) -> Address:
    """Parse a Remmicom-platform location string into Address components.

    Pure function. Extracts the primary street + house number (first segment
    when multiple streets are joined with 'en' or ','), the 4-digit postcode,
    the municipality, and the first cadastral parcel reference normalized to
    'afdeling N sectie X nr. NNN' ('afdeling N' omitted when the source
    string lacks it). Handles the Antwerpen "… te 2000 Antwerpen" connective
    and the "zn" (zonder nummer) marker. ``raw`` is preserved untouched;
    ``point`` stays None for the geocoder to fill. Unparseable parts degrade
    to None.

    Named after the Gent source where the format was first observed; the
    format is Remmicom-platform-wide.
    """
    text = raw.strip()
    if not text:
        return Address(raw=raw)

    # --- parcel: first parenthesized cadastral reference ---
    parcel_id: str | None = None
    pm = _PARCEL_RE.search(text)
    if pm:
        afdeling, sectie, nr = pm.groups()
        nr = re.sub(r"\s+", " ", nr).strip()
        if nr:
            prefix = f"afdeling {afdeling} " if afdeling else ""
            parcel_id = f"{prefix}sectie {sectie.upper()} nr. {nr}"

    # --- postcode + municipality ---
    postcode: str | None = None
    municipality: str | None = None
    street_part = text
    zm = _POSTCODE_RE.search(text)
    if zm:
        postcode = zm.group(1)
        # Municipality runs until a connective 'en' (e.g. "Gent en (afdeling ...")
        muni = re.split(r"\s+en\b", zm.group(2))[0].strip(" ,")
        municipality = muni or None
        street_part = text[: zm.start()]

    # --- primary street + house number ---
    street: str | None = None
    house_number: str | None = None
    # Strip stray ", ," artifacts and the trailing comma before the postcode
    street_part = re.sub(r"[\s,]+$", "", street_part).strip()
    # Antwerpen joins street and postcode with "te": "Eikenstraat 2 te 2000 …"
    street_part = re.sub(r"\s+te$", "", street_part)
    if street_part:
        primary = re.split(r"\s+en\s+", street_part)[0].strip(" ,")
        # Multi-address tenants also join with "," ("Lange Nieuwstraat 64-68,
        # Eikenstraat 2") — the primary address is the first comma segment.
        primary = primary.split(",")[0].strip()
        # "zn" / "z/n" = zonder nummer (no house number)
        primary = re.sub(r"\s+z\.?/?n\.?$", "", primary, flags=re.IGNORECASE)
        hm = _HOUSE_NUMBER_RE.match(primary)
        if hm:
            street = hm.group(1).strip(" ,") or None
            house_number = re.sub(r"\s+", "", hm.group(2))
        else:
            street = primary or None

    return Address(
        raw=raw,
        street=street,
        house_number=house_number,
        postcode=postcode,
        municipality=municipality,
        parcel_id=parcel_id,
    )


def address_geocode_query(address: Address) -> str:
    """Build a geocoder query from parsed components, falling back to raw."""
    if address.street and address.postcode:
        left = " ".join(p for p in (address.street, address.house_number) if p)
        right = " ".join(p for p in (address.postcode, address.municipality) if p)
        return f"{left}, {right}"
    if address.street and address.municipality:
        left = " ".join(p for p in (address.street, address.house_number) if p)
        return f"{left}, {address.municipality}"
    return address.raw


# .NET ticks are 100-ns intervals since 0001-01-01; the platform's 2026
# release emits them in data-timestamp instead of epoch seconds.
_DOTNET_TICKS_EPOCH_OFFSET_S = 62_135_596_800


def _parse_date_attr(value: str | None) -> date | None:
    """Parse a data-timestamp attribute (epoch seconds or .NET ticks) to a date."""
    if not value:
        return None
    try:
        ts = int(value)
        if ts > 10**12:  # .NET ticks (epoch seconds are ~1.7e9)
            ts = ts // 10**7 - _DOTNET_TICKS_EPOCH_OFFSET_S
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except (ValueError, OSError):
        return None


def _parse_nl_date(day: str, month_name: str, year: str) -> date | None:
    """Parse a Dutch prose date ('12', 'juni', '2026') to a date."""
    month = _NL_MONTHS.get(month_name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tenant registry — slugging, seed list, discovery
# ---------------------------------------------------------------------------

def municipality_slug(name: str) -> str:
    """Slug a municipality name into its consultatieomgeving.net subdomain.

    Verified live 2026-06-12: lowercase, diacritics stripped, hyphens kept,
    spaces become hyphens ("Sint-Truiden" → sint-truiden, "Heist-op-den-Berg"
    → heist-op-den-berg, "Sint-Pieters-Leeuw" → sint-pieters-leeuw).
    """
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"['’ʼ]", "", s)  # drop apostrophes
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def slug_candidates(name: str) -> list[str]:
    """Candidate subdomains for a municipality, most likely first.

    The hyphenated form is platform-canonical; a concatenated variant is
    probed second for names containing separators (defensive — no live
    tenant has needed it so far).
    """
    primary = municipality_slug(name)
    candidates = [primary]
    concatenated = primary.replace("-", "")
    if concatenated != primary:
        candidates.append(concatenated)
    return candidates


def tenants_file_path(settings) -> Path:
    """Resolve the persisted tenant-list JSON path."""
    configured = getattr(settings, "consultatie_tenants_file", None)
    if configured:
        return Path(configured)
    return settings.data_root / "consultatie_tenants.json"


def load_tenants(settings) -> list[str]:
    """Tenant slugs to scrape: seed list ∪ discovered list (if persisted)."""
    slugs: set[str] = set(settings.consultatie_seed_tenants)
    path = tenants_file_path(settings)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            slugs.update(
                t["slug"] for t in data.get("tenants", []) if t.get("slug")
            )
        except (json.JSONDecodeError, OSError, TypeError, KeyError) as exc:
            log.warning(
                "consultatie_tenants_file_unreadable",
                path=str(path),
                error=str(exc),
            )
    return sorted(slugs)


_GEMEENTEN_PATH = "/v2/gemeenten?status=inGebruik&limit=100"


async def list_flemish_municipalities(settings) -> list[str]:
    """Enumerate Flemish municipality names from the official basisregisters API.

    Open data, no key. Paginates via the 'volgende' link until exhausted.
    """
    client = create_http_client(settings, source="basisregisters")
    names: list[str] = []
    try:
        url: str | None = _GEMEENTEN_PATH
        while url:
            response = await client.get(url)
            data = response.json()
            for gemeente in data.get("gemeenten", []):
                naam = (
                    gemeente.get("gemeentenaam", {})
                    .get("geografischeNaam", {})
                    .get("spelling")
                )
                if naam:
                    names.append(naam)
            url = data.get("volgende")
    finally:
        await client.aclose()
    # Dedupe, preserving order
    seen: set[str] = set()
    unique = [n for n in names if not (n in seen or seen.add(n))]
    return unique


def _looks_like_consultatie(html: str) -> bool:
    """Heuristic: the page is the Remmicom Consultatie Module Burger index."""
    return "Remmicom" in html or 'class="list"' in html


async def discover_tenants(settings, *, limit: int | None = None) -> list[dict]:
    """Probe every Flemish municipality for a live consultatieomgeving tenant.

    1 req/2 s with the identified UA (the 'vlaanderen_consultatie' throttle).
    Persists the live list as JSON at tenants_file_path(settings) and returns
    it. ~285 municipalities ≈ 10 minutes.
    """
    names = await list_flemish_municipalities(settings)
    if limit is not None:
        names = names[:limit]

    client = create_http_client(settings, source="vlaanderen_consultatie")
    tenants: list[dict] = []
    try:
        for name in names:
            for slug in slug_candidates(name):
                url = (
                    settings.consultatie_site_template.format(slug=slug)
                    + "/burger" + _INDEX_PATH
                )
                try:
                    response = await client.get(url)
                except httpx.HTTPError:
                    continue  # 404 / network error → not a tenant under this slug
                if response.status_code == 200 and _looks_like_consultatie(
                    response.text
                ):
                    tenants.append({"municipality": name, "slug": slug})
                    log.info(
                        "consultatie_tenant_found", municipality=name, slug=slug
                    )
                    break
    finally:
        await client.aclose()

    path = tenants_file_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "probed": len(names),
                "count": len(tenants),
                "tenants": tenants,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    log.info(
        "consultatie_discovery_done",
        probed=len(names),
        live=len(tenants),
        path=str(path),
    )
    return tenants


# ---------------------------------------------------------------------------
# Tolerant label extraction — per-field synonym lists across three forms
# ---------------------------------------------------------------------------

# Per-field label synonyms observed across tenants (Gent / Antwerpen /
# Hasselt / Aalter). Extend here when a new tenant drifts; SchemaDriftError
# fires only when NO synonym (and no prose fallback) matches.
LABEL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "omv_reference": (
        "Projectnummer",
        "Omgevingsloket nummer",
        "Omgevingsloketnummer",
        "Omgevingsvergunning",
        "Projectnummer omgevingsloket",
    ),
    "title": (
        "Onderwerp",
        "Voorwerp van de aanvraag",
        "Voorwerp",
        "Kort omschreven gaat het over",
        "Korte omschrijving",
        "Omschrijving van de aanvraag",
    ),
    "project_type": (
        "Aard van de aanvraag",
        "Vergunningsplichten",
        "Type aanvraag",
        "Aard aanvraag",
        "Type van de aanvraag",
    ),
    "location": (
        "Locatie(s)",
        "Ligging van het project",
        "Ligging",
        "Locatie",
        "Adres(sen)",
    ),
    "parcel": (
        "Kadastrale gegevens",
        "Kadastrale ligging",
        "Kadastrale omschrijving",
    ),
    "authority": (
        "Vergunningverlenende overheid",
        "Bevoegde overheid",
    ),
}

# Prose fallbacks (Hasselt / Aalter free-text bekendmakingen)
_OMV_REF_RE = re.compile(r"\bOMV_\d{6,}\b")
_GELEGEN_RE = re.compile(
    r"gelegen\s+(?:te|aan|in)\s+(.{3,200}?)(?:\s*,\s*kadastraal|\s+kadastraal|\.\s|\.$)",
    re.IGNORECASE,
)
# "… in <location> en gekend in het kadaster …" — tempered so the group
# cannot span an inner " in ", anchoring to the LAST 'in' before 'kadaster'.
_KADASTER_LOC_RE = re.compile(
    r"\bin\s+((?:(?!\bin\s).){3,200}?)\s+en\s+gekend\s+in\s+het\s+kadaster",
    re.IGNORECASE,
)
# The sentence-period terminator must not fire on the dot in the 'nr.'/'nrs.'
# abbreviations that appear inside every cadastral reference.
_KADASTRAAL_RE = re.compile(
    r"(?:kadastraal\s+gekend(?:\s+als)?|gekend\s+in\s+het\s+kadaster\s+als)[\s:]+"
    r"(.{3,200}?)\s*(?:(?<!nr)(?<!nrs)\.(?:\s|$)|$)",
    re.IGNORECASE,
)
_AANVRAAG_VOOR_RE = re.compile(
    r"aanvraag\s+(?:van\s+een\s+omgevingsvergunning\s+)?"
    r"(?:werd\s+)?(?:in(?:gediend)?\s+)?voor\s+(?:het\s+|de\s+|een\s+)?"
    r"(.{3,160}?)(?:\s+in\s+[A-Z]|[.,;])",
)


def find_labelled_value(soup: BeautifulSoup, labels: Iterable[str]) -> str | None:
    """Find a labelled value across the three structural forms.

    For each label synonym, in order:
      1. <dt>Label</dt><dd>value</dd>            (Gent legacy)
      2. <td>Label:</td><td>value</td> table row (Antwerpen Word table)
      3. <p>Label: value</p> or a header <p>Label</p> + value <p>  (Word
         paragraph export, Hasselt "Label : value" included)

    Returns the first non-empty value, else None.
    """
    for label in labels:
        # 1. dt/dd
        dt = soup.find(
            "dt", string=lambda s, _l=label: s is not None and _l in s
        )
        if dt is not None:
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                value = dd.get_text(strip=True)
                if value:
                    return value
            continue

        # 2. table row: label cell → next cell
        found_table = False
        for cell in soup.find_all(["td", "th"]):
            cell_text = cell.get_text(" ", strip=True)
            if cell_text.rstrip(":").strip() != label:
                continue
            found_table = True
            nxt = cell.find_next_sibling(["td", "th"])
            if nxt is not None:
                value = nxt.get_text(" ", strip=True)
                if value:
                    return value
        if found_table:
            continue

        # 3. paragraphs
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            if not text.startswith(label):
                continue
            rest = text[len(label):].strip()
            if rest.startswith(":"):
                rest = rest[1:].strip()
            if rest:
                return rest
            nxt_p = p.find_next_sibling("p")
            if nxt_p is not None:
                value = nxt_p.get_text(" ", strip=True)
                if value:
                    return value
    return None


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_detail_fields(
    soup: BeautifulSoup, *, source_key: str, url: str
) -> dict[str, str | None]:
    """Extract the labelled fields from a detail page, tolerantly.

    Returns dict with keys: omv_reference (required — SchemaDriftError when
    no synonym nor the OMV_ regex matches), title, project_type,
    locatie_text, parcel_text. Optional fields degrade to None.

    Raises DossierVanishedError when the page is the platform's not-found
    page (served with HTTP 200, so the client's raise_for_status never
    fires) — dossiers withdrawn between index and detail pass land here.
    """
    title_tag = soup.title
    if title_tag is not None and "pagina niet gevonden" in title_tag.get_text(
        " ", strip=True
    ).lower():
        raise DossierVanishedError(
            f"{source_key}: detail page vanished (platform not-found page) at {url}"
        )

    page_text = soup.get_text(" ", strip=True)

    omv_reference = find_labelled_value(soup, LABEL_SYNONYMS["omv_reference"])
    if omv_reference is not None:
        m = _OMV_REF_RE.search(omv_reference)
        if m:
            omv_reference = m.group(0)
    if omv_reference is None:
        m = _OMV_REF_RE.search(page_text)
        if m:
            omv_reference = m.group(0)
    if omv_reference is None:
        raise SchemaDriftError(
            f"{source_key}: no omv-reference label "
            f"{list(LABEL_SYNONYMS['omv_reference'])} nor OMV_ pattern found at {url}"
        )

    title = find_labelled_value(soup, LABEL_SYNONYMS["title"])
    if title is None:
        # Fall back to the platform header (always present), minus boilerplate.
        header = soup.find(id="detailHeader")
        if header is not None:
            header_text = _clean_ws(header.get_text(" ", strip=True))
            header_text = re.sub(
                r"^bekendmaking\s+openbaar\s+onderzoek\s*-?\s*",
                "",
                header_text,
                flags=re.IGNORECASE,
            ).strip()
            title = header_text or None

    project_type = find_labelled_value(soup, LABEL_SYNONYMS["project_type"])
    if project_type is None:
        m = _AANVRAAG_VOOR_RE.search(page_text)
        if m:
            project_type = _clean_ws(m.group(1))

    locatie_text = find_labelled_value(soup, LABEL_SYNONYMS["location"]) or ""
    if not locatie_text.strip(" ,"):
        m = _GELEGEN_RE.search(page_text)
        if m is None:
            m = _KADASTER_LOC_RE.search(page_text)
        locatie_text = _clean_ws(m.group(1)) if m else ""
    # Strip degenerate "," placeholder
    if locatie_text.strip() == ",":
        locatie_text = ""

    parcel_text = find_labelled_value(soup, LABEL_SYNONYMS["parcel"])
    if parcel_text is None:
        m = _KADASTRAAL_RE.search(page_text)
        if m:
            parcel_text = _clean_ws(m.group(1))

    return {
        "omv_reference": omv_reference,
        "title": title,
        "project_type": project_type,
        "locatie_text": locatie_text,
        "parcel_text": parcel_text,
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class ConsultatieSource(Source):
    """Multi-tenant scraper for the Remmicom consultation platform.

    index_pass iterates the configured/discovered tenants (robots.txt gated)
    and yields '<slug>/<uuid>' identifiers; detail_pass resolves the tenant
    from the identifier. Gent keeps its historical external ids
    ('gent:OMV_…'), source label ('gent_consultatie'), raw-HTML directory and
    bare-uuid inquiry ids; every other tenant gets '<slug>:OMV_…' and source
    'vlaanderen_consultatie'.
    """

    source_key: ClassVar[str] = "vlaanderen_consultatie"

    def __init__(self, settings, tenants: list[str] | None = None) -> None:
        super().__init__(settings)
        # ASP.NET emits NSC_* cookies; persistent jar prevents 403s on Bestand
        # (httpx scopes cookies per domain, so one jar serves all tenants).
        self._cookies = httpx.Cookies()
        self._tenants: list[str] = tenants or load_tenants(settings)
        # Inquiry periods harvested from index cards (remOoDatum* spans) —
        # some tenants (e.g. Aalter) state the period only on the index.
        self._index_periods: dict[str, tuple[date | None, date | None]] = {}

    # --- tenant URL helpers -------------------------------------------------

    def _burger_base(self, slug: str) -> str:
        """Tenant base URL ending in /burger (no trailing slash)."""
        if slug == "gent":
            return self._settings.gent_consultatie_base.rstrip("/")
        return (
            self._settings.consultatie_site_template.format(slug=slug).rstrip("/")
            + "/burger"
        )

    def _site_root(self, slug: str) -> str:
        parsed = urlparse(self._burger_base(slug))
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _source_label(slug: str) -> str:
        return "gent_consultatie" if slug == "gent" else "vlaanderen_consultatie"

    def _raw_dir(self, slug: str, uuid: str) -> Path:
        if slug == "gent":
            return self._settings.data_root / "raw" / "gent_consultatie" / uuid
        return (
            self._settings.data_root / "raw" / "vlaanderen_consultatie" / slug / uuid
        )

    # --- robots gate ----------------------------------------------------------

    async def _tenant_allowed(self, slug: str) -> bool:
        """Re-check robots.txt at runtime; skip tenants that gain a disallow.

        404 (the platform-wide norm, verified 2026-06-12) → allowed. A 200
        robots.txt is parsed and our index path checked for the identified UA.
        """
        robots_url = f"{self._site_root(slug)}/robots.txt"
        try:
            response = await self._client.get(robots_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return True
            log.warning(
                "consultatie_robots_unreadable",
                tenant=slug,
                status=exc.response.status_code,
            )
            return True  # 4xx other than 404 → no usable robots → allowed
        except httpx.HTTPError as exc:
            log.warning(
                "consultatie_robots_fetch_failed", tenant=slug, error=str(exc)
            )
            return False  # network trouble — skip tenant this run

        parser = robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        ua = self._settings.nominatim_user_agent
        target = f"{self._burger_base(slug)}{_INDEX_PATH}"
        if not parser.can_fetch(ua, target):
            log.warning("consultatie_tenant_robots_disallow", tenant=slug)
            return False
        return True

    # --- index ----------------------------------------------------------------

    async def _index_for_tenant(
        self, slug: str, *, limit: int | None
    ) -> AsyncIterator[str]:
        """Yield UUIDs of active public inquiry dossiers for one tenant.

        Raises SchemaDriftError when the index markup drifted.
        """
        index_url = f"{self._burger_base(slug)}{_INDEX_PATH}"
        response = await self._client.get(index_url)
        soup = BeautifulSoup(response.text, "html.parser")

        ul = soup.find("ul", class_="list")
        if ul is None:
            raise SchemaDriftError(
                f"{slug}: <ul class=list> not found at {index_url}"
            )

        count = 0
        for li in ul.find_all("li"):
            title_el = li.find(class_="remTitel")
            if title_el is None:
                continue
            # The Details anchor moved from inside .remTitel to a sibling of it;
            # search the whole card and match on the Details path so both
            # markup variants (and absolute vs relative hrefs) resolve.
            anchor = li.find(
                "a",
                href=lambda h: h is not None
                and "/OpenbareOnderzoeken/Details/" in h,
            )
            if anchor is None:
                continue
            href = anchor["href"]
            # href like "/burger/nl/OpenbareOnderzoeken/Details/<uuid>" (or absolute)
            parts = href.rstrip("/").split("/")
            uuid = parts[-1] if parts else None
            if not uuid:
                continue
            # Harvest the inquiry period from the card — some tenants state
            # it ONLY here (data-timestamp is epoch seconds or .NET ticks).
            van_el = li.find(class_="remOoDatumVan")
            tot_el = li.find(class_="remOoDatumTot")
            if van_el is not None and tot_el is not None:
                self._index_periods[f"{slug}/{uuid}"] = (
                    _parse_date_attr(van_el.get("data-timestamp")),
                    _parse_date_attr(tot_el.get("data-timestamp")),
                )
            yield uuid
            count += 1
            if limit is not None and count >= limit:
                return

    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield '<slug>/<uuid>' across all tenants (limit = total cap)."""
        remaining = limit
        for slug in self._tenants:
            if remaining is not None and remaining <= 0:
                return
            try:
                if not await self._tenant_allowed(slug):
                    continue
            except Exception as exc:  # noqa: BLE001 — one tenant must not kill the sweep
                log.warning(
                    "consultatie_robots_check_failed", tenant=slug, error=str(exc)
                )
                continue
            try:
                count = 0
                async for uuid in self._index_for_tenant(slug, limit=remaining):
                    yield f"{slug}/{uuid}"
                    count += 1
                if remaining is not None:
                    remaining -= count
                log.info("consultatie_tenant_indexed", tenant=slug, dossiers=count)
            except SchemaDriftError as exc:
                log.error("consultatie_index_schema_drift", tenant=slug, error=str(exc))
            except httpx.HTTPError as exc:
                log.warning(
                    "consultatie_index_fetch_failed", tenant=slug, error=str(exc)
                )

    # --- detail ----------------------------------------------------------------

    async def detail_pass(
        self, identifier: str
    ) -> tuple[PermitProject, PublicInquiry | None]:
        """Fetch and parse a single dossier; identifier is '<slug>/<uuid>'."""
        slug, sep, uuid = identifier.partition("/")
        if not sep or not uuid:
            raise ValueError(
                f"consultatie identifier must be '<slug>/<uuid>', got {identifier!r}"
            )
        return await self._detail_for_tenant(slug, uuid)

    async def _detail_for_tenant(
        self, slug: str, uuid: str
    ) -> tuple[PermitProject, PublicInquiry | None]:
        burger_base = self._burger_base(slug)
        site_root = self._site_root(slug)
        detail_url = f"{burger_base}{_DETAIL_PATH.format(uuid=uuid)}"
        response = await self._client.get(detail_url, cookies=self._cookies)
        html = response.text
        content_hash = hashlib.sha256(html.encode()).hexdigest()

        soup = BeautifulSoup(html, "html.parser")
        fields = parse_detail_fields(
            soup, source_key=self._source_label(slug), url=detail_url
        )
        omv_reference = fields["omv_reference"]
        assert omv_reference is not None  # parse_detail_fields guarantees
        title_text = fields["title"]
        locatie_text = fields["locatie_text"] or ""

        # --- Save raw HTML ---
        raw_dir = self._raw_dir(slug, uuid)
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_html_path = raw_dir / "detail.html"
        raw_html_path.write_text(html, encoding="utf-8")

        # --- PDF attachments ---
        pdf_dir = raw_dir / "pdfs"
        attachments: list[str] = []
        dossier_pdfs: list[Path] = []

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
                        "consultatie_pdf_download_failed",
                        tenant=slug,
                        url=abs_url,
                        error=str(exc),
                    )

        # --- Description: title → first PDF text → None ---
        description: str | None = title_text if title_text else None
        if not description and dossier_pdfs:
            try:
                from debouw.ingest.pdf import extract_text
                pdf_text = await extract_text(dossier_pdfs[0])
                description = pdf_text[:500] if pdf_text else None
            except NotImplementedError:
                description = None

        # --- Public inquiry period ---
        # Chain: detail-page remOoDatum* spans → prose regex → index-card
        # cache (tenants like Aalter state the window only on the index).
        period_start: date | None = None
        period_end: date | None = None
        datum_van_el = soup.find(class_="remOoDatumVan")
        datum_tot_el = soup.find(class_="remOoDatumTot")
        if datum_van_el is not None and datum_tot_el is not None:
            period_start = _parse_date_attr(datum_van_el.get("data-timestamp"))
            period_end = _parse_date_attr(datum_tot_el.get("data-timestamp"))
        if period_start is None or period_end is None:
            m = _PERIODE_RE.search(soup.get_text(" ", strip=True))
            if m:
                period_start = _parse_nl_date(m.group(1), m.group(2), m.group(3))
                period_end = _parse_nl_date(m.group(4), m.group(5), m.group(6))
        if period_start is None or period_end is None:
            cached = self._index_periods.get(f"{slug}/{uuid}")
            if cached is not None:
                period_start, period_end = cached

        inquiry: PublicInquiry | None = None
        if period_start and period_end:
            days_remaining = (period_end - date.today()).days
            inquiry = PublicInquiry(
                external_id=uuid if slug == "gent" else f"{slug}:{uuid}",
                period_start=period_start,
                period_end=period_end,
                objection_deadline=period_end,
                days_remaining=days_remaining if days_remaining >= 0 else None,
            )
            status = PermitProjectStatus.IN_PUBLIC_INQUIRY
        else:
            status = PermitProjectStatus.INTAKE

        # --- Address ---
        address = parse_gent_address(locatie_text)
        updates: dict[str, str] = {}
        parcel_text = fields["parcel_text"]
        if address.parcel_id is None and parcel_text:
            updates["parcel_id"] = parcel_text
        if address.municipality is None:
            # The tenant IS the municipality — str.title() restores the
            # display casing for hyphenated slugs ("sint-truiden" → "Sint-Truiden").
            updates["municipality"] = slug.title()
        if updates:
            address = address.model_copy(update=updates)

        now = datetime.now(timezone.utc)
        external_id = f"{slug}:{omv_reference}"
        public_detail_url = f"{burger_base}/nl/OpenbareOnderzoeken/Details/{uuid}"

        project = PermitProject(
            external_id=external_id,
            source=self._source_label(slug),
            omv_reference=omv_reference,
            detail_url=public_detail_url,
            title=title_text or omv_reference,
            description=description,
            # GDPR: never persist the applicant, even when a tenant's prose
            # displays it (posture identical to Gent).
            applicant_name=None,
            address=address,
            project_type=fields["project_type"],
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


class GentSource(ConsultatieSource):
    """Single-tenant Gent scraper (backward-compatible).

    Same parser as ConsultatieSource, pinned to the gent tenant: bare-uuid
    identifiers, 'gent:OMV_…' external ids, source 'gent_consultatie' and the
    historical raw-HTML directory. Index schema drift raises (it is the
    canary tenant), unlike the multi-tenant sweep which logs and skips.
    """

    source_key: ClassVar[str] = "gent"

    def __init__(self, settings) -> None:
        super().__init__(settings, tenants=["gent"])

    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield UUIDs of active public inquiry dossiers."""
        async for uuid in self._index_for_tenant("gent", limit=limit):
            yield uuid

    async def detail_pass(
        self, uuid: str
    ) -> tuple[PermitProject, PublicInquiry | None]:
        """Fetch and parse a single dossier detail page."""
        return await self._detail_for_tenant("gent", uuid)
