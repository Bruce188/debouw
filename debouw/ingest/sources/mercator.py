"""
Vlaanderen-wide ingestion via the MercatorNet WFS — the statewide discovery spine.

Harvests omgevingsvergunning dossiers for ALL of Flanders from the official
MercatorNet "raadpleegdiensten" WFS (departement Omgeving). WFS 2.0.0 with
result paging (count + startIndex), GeoJSON output, no auth, no bot protection.

Layers used:
- ``lu:lu_omv_gd_v2``   — stedenbouwkundige handelingen (primary discovery
  layer, ~765k features statewide; ~22.8k "In behandeling" as of 2026-06-12).
- ``lu:lu_omv_iioa_v2`` — ingedeelde inrichtingen of activiteiten (IIOA).
  Joined per page on ``projectnummer`` (CQL ``IN``) to populate
  ``iioa_class`` from ``klasse_iioa`` — feeds the MER/IIOA risk heuristics.

KNOWN GAP (verified against the live service on 2026-06-12):
``huidige_toestand`` has NO "openbaar onderzoek" state and the layers expose
no inquiry start/end dates — public-inquiry windows are NOT available from
this source. ``detail_pass`` therefore always returns ``inquiry=None``;
inquiry data must come from per-municipality portals (e.g. Gent) or the
Inzageloket. "In behandeling" dossiers map to INTAKE/APPEALED only.

Also no street-level address is provided — only the deciding authority
(``vvo_huidige_toestand``, which may be a municipality OR a province /
regional authority) and the parcel geometry. The Address is built honestly
from what exists: projectnummer + authority in ``raw``, municipality only
when the authority is an actual municipality, and the point from the
Lambert-72 parcel-geometry centroid (reusing the Brussels conversion).

ToS / GDPR:
- Open data — licence "modellicentie gratis hergebruik v1.0" (free reuse);
  see the endpoint constant below.
- Identified User-Agent (nominatim_user_agent setting), 1 req/2 s throttle.
- ``aanvrager`` IS displayed by the source and natural persons are already
  anonymized server-side ("Natuurlijk perso(o)n(en)"), so persisting it
  complies with the "only persist if source displays it" rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable
from datetime import date, datetime, timezone
from typing import Any, ClassVar

import structlog

from debouw.ingest.sources.base import SchemaDriftError, Source

# REUSE (commit 4d1edea): Lambert-72 → WGS84 centroid conversion lives in the
# Brussels source; we normalize MultiPolygon → Polygon shape and delegate.
from debouw.ingest.sources.brussels import _lambert72_centroid_to_wgs84
from debouw.models.permit import (
    Address,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
    PublicInquiry,
)

log = structlog.get_logger(__name__)

# MercatorNet public WFS (host-only base URL in Settings.mercator_base).
# Endpoint: https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/wfs
# Licence: "modellicentie gratis hergebruik v1.0" — Flemish model licence for
# free reuse of open government data. Polite identified access regardless.
_WFS_PATH = "/raadpleegdienstenmercatorpubliek/wfs"

_GD_LAYER = "lu:lu_omv_gd_v2"  # stedenbouwkundige handelingen (primary)
_IIOA_LAYER = "lu:lu_omv_iioa_v2"  # IIOA — provides klasse_iioa

# Active dossiers only: "In behandeling (in eerste aanleg)" / "(na beroep)" / ...
_CQL_ACTIVE = "huidige_toestand LIKE 'In behandeling%'"
# datum_huidige_toestand updates at least daily; DESC sort means a capped
# incremental run sees the most recently changed dossiers first, and the
# pipeline's content_hash gate skips the unchanged ones.
_SORT_BY = "datum_huidige_toestand DESC"

# projectnummer values are interpolated into CQL — restrict the alphabet so a
# drifted/poisoned value can never break out of the quoted IN list.
_PROJECTNUMMER_SAFE = re.compile(r"^[0-9A-Za-z_\-]+$")

_OMGEVINGSLOKET_PROJECT_URL = (
    "https://omgevingsloket.omgeving.vlaanderen.be?project={nr}"
)

_TITLE_MAX_LEN = 200

# Authorities in vvo_huidige_toestand that are NOT municipalities.
_NON_MUNICIPAL_VVO_PREFIXES = ("provincie ", "vlaams", "de vlaamse", "gewestelijk")


def _municipality_from_vvo(vvo: str | None) -> str | None:
    """Return the municipality when the deciding authority is one.

    ``vvo_huidige_toestand`` holds the vergunningverlenende overheid: usually
    the municipality, but provincial dossiers carry "Provincie X" and regional
    dossiers a Flemish-government authority. Returning those as municipality
    would corrupt the dashboard's gemeente filter — be honest, return None.
    """
    if not vvo:
        return None
    lowered = vvo.strip().lower()
    if lowered.startswith(_NON_MUNICIPAL_VVO_PREFIXES):
        return None
    return vvo.strip()


def _status_from_toestand(toestand: str | None) -> PermitProjectStatus:
    """Map huidige_toestand to PermitProjectStatus.

    No "openbaar onderzoek" state exists in this source (KNOWN GAP), so
    IN_PUBLIC_INQUIRY is never produced here.
    """
    lowered = (toestand or "").lower()
    if "beroep" in lowered:
        # "In behandeling (na beroep)" / "Beroep ingediend" — appeal stage
        return PermitProjectStatus.APPEALED
    if any(kw in lowered for kw in ("vergund", "geweigerd", "beslist", "besliss")):
        return PermitProjectStatus.DECIDED
    if any(
        kw in lowered
        for kw in ("afgesloten", "stopgezet", "ingetrokken", "vervallen", "onontvankelijk")
    ):
        return PermitProjectStatus.CLOSED
    # "In behandeling (in eerste aanleg)" and anything unknown → intake
    return PermitProjectStatus.INTAKE


def _parse_iso_date(value: str | None) -> date | None:
    """Parse 'YYYY-MM-DD' (tolerates trailing time component) to a date."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _geometry_centroid_to_wgs84(geometry: dict | None) -> GeoPoint | None:
    """Centroid of a Lambert-72 (Multi)Polygon converted to WGS84.

    The Mercator layers serve MultiPolygon in EPSG:31370. The shared Brussels
    helper expects Polygon-shaped ``{"coordinates": [ring, ...]}``, so for a
    MultiPolygon every polygon's outer ring is merged into one point cloud
    (naive mean centroid, same approach as Brussels) before delegating.
    """
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if geometry.get("type") == "MultiPolygon":
        try:
            merged_ring = [pt for polygon in coords for pt in polygon[0]]
        except (TypeError, IndexError):
            return None
        if not merged_ring:
            return None
        return _lambert72_centroid_to_wgs84({"coordinates": [merged_ring]})
    return _lambert72_centroid_to_wgs84(geometry)


class MercatorSource(Source):
    """Statewide Flanders harvester for the MercatorNet omgevingsloket layers.

    Unlike the HTML scrapers, one WFS page already contains the full feature
    payload — ``index_pass`` caches each page's features and ``detail_pass``
    builds the PermitProject from that cache without extra HTTP calls (a
    standalone ``detail_pass`` falls back to a single-feature CQL fetch).
    """

    source_key: ClassVar[str] = "mercator"

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._feature_cache: dict[str, dict] = {}
        self._iioa_classes: dict[str, int] = {}

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get_feature(self, **extra_params: Any) -> dict:
        """Issue a WFS GetFeature request and return the parsed GeoJSON dict."""
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "outputFormat": "application/json",
            **extra_params,
        }
        response = await self._client.get(_WFS_PATH, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SchemaDriftError(
                f"mercator: non-JSON GetFeature response at {_WFS_PATH} "
                f"(typeNames={extra_params.get('typeNames')}): {exc}"
            ) from exc
        if not isinstance(payload, dict) or "features" not in payload:
            raise SchemaDriftError(
                f"mercator: 'features' key missing in GetFeature response at "
                f"{_WFS_PATH} (typeNames={extra_params.get('typeNames')})"
            )
        return payload

    async def _fetch_gd_page(self, start_index: int, count: int) -> dict:
        return await self._get_feature(
            typeNames=_GD_LAYER,
            CQL_FILTER=_CQL_ACTIVE,
            sortBy=_SORT_BY,
            count=count,
            startIndex=start_index,
        )

    async def _fetch_iioa_classes(
        self, projectnummers: Iterable[str]
    ) -> dict[str, int]:
        """Join the IIOA layer on projectnummer; return {projectnummer: klasse}."""
        safe = [nr for nr in projectnummers if _PROJECTNUMMER_SAFE.match(nr)]
        if not safe:
            return {}
        cql = "projectnummer IN ({})".format(
            ",".join(f"'{nr}'" for nr in safe)
        )
        payload = await self._get_feature(
            typeNames=_IIOA_LAYER,
            CQL_FILTER=cql,
            count=len(safe) * 4,  # a project can carry several IIOA features
        )
        classes: dict[str, int] = {}
        for feature in payload["features"]:
            props = feature.get("properties") or {}
            nr = props.get("projectnummer")
            klasse = props.get("klasse_iioa")
            if not nr or klasse is None:
                continue
            try:
                klasse_int = int(klasse)
            except (TypeError, ValueError):
                continue
            # Keep the strictest (lowest) class when multiple IIOA features exist
            if nr not in classes or klasse_int < classes[nr]:
                classes[nr] = klasse_int
        return classes

    # ------------------------------------------------------------------
    # Source interface
    # ------------------------------------------------------------------

    async def index_pass(self, *, limit: int | None) -> AsyncIterator[str]:
        """Yield projectnummers of active dossiers, newest state change first.

        Pages via WFS result paging (count + startIndex). Deduplicates on
        projectnummer — one project may span multiple voorwerp features, and
        live data can shift between page requests. When ``limit`` is None the
        modest ``mercator_default_limit`` applies; a full statewide harvest
        (~22k active dossiers) is opt-in via an explicit large --limit or
        MERCATOR_DEFAULT_LIMIT<=0.
        """
        page_size = max(1, self._settings.mercator_page_size)
        effective_limit: int | None = (
            limit if limit is not None else self._settings.mercator_default_limit
        )
        if effective_limit is not None and effective_limit <= 0:
            effective_limit = None  # opt-in full harvest

        start_index = 0
        yielded = 0
        seen: set[str] = set()

        while True:
            payload = await self._fetch_gd_page(start_index, page_size)
            features = payload["features"]
            if not features:
                break

            new_nrs: list[str] = []
            for feature in features:
                props = feature.get("properties") or {}
                nr = props.get("projectnummer")
                if not nr:
                    raise SchemaDriftError(
                        "mercator: projectnummer missing in feature at "
                        f"startIndex={start_index} ({_GD_LAYER})"
                    )
                if nr not in seen:
                    new_nrs.append(nr)

            if self._settings.mercator_iioa_join and new_nrs:
                try:
                    self._iioa_classes.update(
                        await self._fetch_iioa_classes(new_nrs)
                    )
                except SchemaDriftError:
                    raise
                except Exception as exc:
                    # IIOA join is enrichment, not discovery — degrade gracefully
                    log.warning("mercator_iioa_join_failed", error=str(exc))

            for feature in features:
                props = feature.get("properties") or {}
                nr = props["projectnummer"]
                if nr in seen:
                    continue
                seen.add(nr)
                self._feature_cache[nr] = feature
                yield nr
                yielded += 1
                if effective_limit is not None and yielded >= effective_limit:
                    return

            if len(features) < page_size:
                break
            start_index += len(features)

    async def detail_pass(
        self, identifier: str
    ) -> tuple[PermitProject, PublicInquiry | None]:
        """Build a PermitProject from the cached WFS feature.

        Zero extra HTTP calls in the normal index→detail flow. A direct call
        without a prior index_pass falls back to a single-feature CQL fetch.
        Always returns ``inquiry=None`` — see module docstring (KNOWN GAP).
        """
        feature = self._feature_cache.pop(identifier, None)
        if feature is None:
            feature = await self._fetch_single_feature(identifier)

        props = feature.get("properties") or {}
        nr = props.get("projectnummer")
        if not nr:
            raise SchemaDriftError(
                f"mercator: projectnummer missing for identifier '{identifier}'"
            )
        toestand = props.get("huidige_toestand")
        if toestand is None:
            raise SchemaDriftError(
                f"mercator: huidige_toestand missing for projectnummer {nr}"
            )

        iioa_class = self._iioa_classes.get(nr)

        # content_hash covers the full feature AND the joined IIOA class, so a
        # state change in either layer defeats the pipeline's idempotency gate.
        hash_payload = json.dumps(
            {"feature": feature, "klasse_iioa": iioa_class},
            sort_keys=True,
            ensure_ascii=False,
        )
        content_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

        # --- Persist the raw feature JSON (raw_html_path contract) ---
        raw_dir = self._settings.data_root / "raw" / "vlaanderen_mercator" / nr
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "feature.json"
        raw_path.write_text(
            json.dumps(feature, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # --- Field mapping ---
        vvo = props.get("vvo_huidige_toestand")
        municipality = _municipality_from_vvo(vvo)
        description = props.get("handeling_omschrijving") or None
        project_type = props.get("omschrijving_projecttype") or None

        if description and len(description) > _TITLE_MAX_LEN:
            title = description[: _TITLE_MAX_LEN - 1] + "…"
        elif description:
            title = description
        else:
            title = f"{project_type or 'Omgevingsproject'} {nr}"

        # Honest address: no street-level address exists in this source.
        raw_parts = [f"projectnummer {nr}"]
        if vvo:
            raw_parts.append(str(vvo))
        address_raw = ", ".join(raw_parts) + " (bron: Mercator WFS, geen adres)"

        geo_point = _geometry_centroid_to_wgs84(feature.get("geometry"))

        datum_indiening = _parse_iso_date(props.get("datum_indiening"))
        decision_regime = (
            "pre_2026_reform"
            if datum_indiening is not None and datum_indiening < date(2026, 1, 1)
            else "post_2026_reform"
        )

        detail_url = props.get("project_link") or _OMGEVINGSLOKET_PROJECT_URL.format(
            nr=nr
        )

        now_utc = datetime.now(timezone.utc)
        project = PermitProject(
            external_id=f"mercator:{nr}",
            source="vlaanderen_mercator",
            region="vl",
            omv_reference=nr,
            detail_url=detail_url,
            title=title,
            description=description,
            # Source displays aanvrager; natural persons pre-anonymized upstream.
            applicant_name=props.get("aanvrager") or None,
            address=Address(
                raw=address_raw,
                municipality=municipality,
                point=geo_point,
            ),
            project_type=project_type,
            iioa_class=iioa_class,
            status=_status_from_toestand(toestand),
            attachments=[],
            dossier_pdfs=[],
            overlays=None,  # pipeline fills via Geopunt enrichment
            raw_html_path=raw_path,
            first_seen_at=now_utc,
            last_changed_at=now_utc,
            content_hash=content_hash,
            decision_regime=decision_regime,
            case_language="nl",
        )
        # KNOWN GAP: no openbaar-onderzoek state or inquiry dates in this
        # source — inquiry is always None.
        return project, None

    # ------------------------------------------------------------------
    # Fallback single-feature fetch
    # ------------------------------------------------------------------

    async def _fetch_single_feature(self, projectnummer: str) -> dict:
        """Fetch one gd-layer feature by projectnummer (cache-miss fallback)."""
        if not _PROJECTNUMMER_SAFE.match(projectnummer):
            raise SchemaDriftError(
                f"mercator: unsafe projectnummer '{projectnummer}' rejected"
            )
        payload = await self._get_feature(
            typeNames=_GD_LAYER,
            CQL_FILTER=f"projectnummer='{projectnummer}'",
            count=1,
        )
        features = payload["features"]
        if not features:
            raise SchemaDriftError(
                f"mercator: projectnummer {projectnummer} not found in {_GD_LAYER}"
            )
        if (
            self._settings.mercator_iioa_join
            and projectnummer not in self._iioa_classes
        ):
            try:
                self._iioa_classes.update(
                    await self._fetch_iioa_classes([projectnummer])
                )
            except Exception as exc:
                log.warning("mercator_iioa_join_failed", error=str(exc))
        return features[0]
