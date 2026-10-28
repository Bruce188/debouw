"""
Tests for debouw.ingest.sources.mercator (statewide Flanders WFS).

Fixture-driven: NO live network. Fixtures under tests/fixtures/vlaanderen_mercator/
are REAL GetFeature responses captured from the live MercatorNet WFS on
2026-06-12 (open data, "modellicentie gratis hergebruik v1.0"), truncated to
3 features per page. gd_page1/gd_page2 share projectnummer 2021091909 — a
genuine cross-page duplicate observed live (data shifted between requests),
which locks the dedupe behaviour against real-world conditions.

Covers:
1.  WFS result paging (count + startIndex) + termination on short/empty page.
2.  Cross-page dedupe on projectnummer.
3.  Default-limit cap (modest default; full harvest opt-in) + explicit --limit.
4.  Field mapping → PermitProject (external_id, source, region, status, regime).
5.  vvo_huidige_toestand municipality handling (province → None).
6.  Lambert-72 MultiPolygon centroid → WGS84 (reuses Brussels conversion).
7.  content_hash idempotency (stable across parses; sensitive to changes).
8.  IIOA join (lu_omv_iioa_v2) → iioa_class.
9.  KNOWN GAP lock: inquiry is always None (no openbaar-onderzoek state).
10. Identified User-Agent on every request.
11. Registry + pipeline e2e (point preserved through the pipeline).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from debouw.config import Settings
from debouw.ingest.sources.mercator import (
    MercatorSource,
    _municipality_from_vvo,
    _status_from_toestand,
)
from debouw.models.permit import PermitProjectStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "vlaanderen_mercator"
_GD_PAGE1 = json.loads((_FIXTURES / "gd_page1.json").read_text(encoding="utf-8"))
_GD_PAGE2 = json.loads((_FIXTURES / "gd_page2.json").read_text(encoding="utf-8"))
_IIOA_JOIN = json.loads((_FIXTURES / "iioa_join.json").read_text(encoding="utf-8"))

_EMPTY_FC = {
    "type": "FeatureCollection",
    "features": [],
    "numberMatched": 0,
    "numberReturned": 0,
}

_WFS_URL_RE = r".*mercator\.vlaanderen\.be.*/wfs.*"


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        data_root=tmp_path / "data",
        db_path=tmp_path / "data" / "debouw.sqlite",
        throttle_mercator_seconds=0.01,
        mercator_page_size=3,  # fixture pages hold 3 features each
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _mock_gd(mock: respx.MockRouter, pages: list[dict]):
    """Mock the gd-layer route: successive calls return successive pages."""
    route = mock.get(
        url__regex=_WFS_URL_RE,
        params__contains={"typeNames": "lu:lu_omv_gd_v2"},
    )
    route.side_effect = [Response(200, json=p) for p in pages]
    return route


def _mock_iioa(mock: respx.MockRouter, payload: dict | None = None):
    """Mock the IIOA-join route (empty join by default)."""
    return mock.get(
        url__regex=_WFS_URL_RE,
        params__contains={"typeNames": "lu:lu_omv_iioa_v2"},
    ).mock(return_value=Response(200, json=payload or _EMPTY_FC))


async def _collect_index(src: MercatorSource, limit: int | None) -> list[str]:
    return [nr async for nr in src.index_pass(limit=limit)]


# ---------------------------------------------------------------------------
# 1+2. Pagination + dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_pages_with_start_index_and_dedupes(tmp_path: Path) -> None:
    """index_pass walks startIndex 0→3→6, stops on empty page, dedupes the
    cross-page duplicate 2021091909."""
    settings = _settings(tmp_path, mercator_default_limit=0)  # unlimited

    with respx.mock(assert_all_called=False) as mock:
        gd_route = _mock_gd(mock, [_GD_PAGE1, _GD_PAGE2, _EMPTY_FC])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=None)

    assert ids == [
        "2021093523",
        "2021093552",
        "2021091909",
        "2021093969",
        "2021094215",
    ], "expected 5 unique projectnummers (duplicate 2021091909 dropped)"

    assert gd_route.call_count == 3
    start_indexes = [
        dict(c.request.url.params)["startIndex"] for c in gd_route.calls
    ]
    assert start_indexes == ["0", "3", "6"]
    # Paging + filter contract (verified live 2026-06-12)
    first_params = dict(gd_route.calls[0].request.url.params)
    assert first_params["count"] == "3"
    assert first_params["CQL_FILTER"] == "huidige_toestand LIKE 'In behandeling%'"
    assert first_params["sortBy"] == "datum_huidige_toestand DESC"


# ---------------------------------------------------------------------------
# 3. Limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_default_limit_caps_statewide_harvest(tmp_path: Path) -> None:
    """limit=None applies the modest mercator_default_limit (full harvest opt-in)."""
    settings = _settings(tmp_path, mercator_default_limit=4)

    with respx.mock(assert_all_called=False) as mock:
        gd_route = _mock_gd(mock, [_GD_PAGE1, _GD_PAGE2, _EMPTY_FC])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=None)

    assert len(ids) == 4
    assert gd_route.call_count == 2  # stopped mid-page-2, page 3 never fetched


@pytest.mark.asyncio
async def test_index_explicit_limit_overrides_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mercator_default_limit=4)

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1, _GD_PAGE2, _EMPTY_FC])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=2)

    assert ids == ["2021093523", "2021093552"]


# ---------------------------------------------------------------------------
# 4. Field mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_maps_fields(tmp_path: Path) -> None:
    """First fixture feature (provincial appeal dossier) maps correctly."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=1)
            project, inquiry = await src.detail_pass(ids[0])

    assert project.external_id == "mercator:2021093523"
    assert project.source == "vlaanderen_mercator"
    assert project.region == "vl"
    assert project.omv_reference == "2021093523"
    assert str(project.detail_url) == (
        "https://omgevingsloket.omgeving.vlaanderen.be/?project=2021093523"
    )
    # "In behandeling (na beroep)" → APPEALED
    assert project.status == PermitProjectStatus.APPEALED
    # datum_indiening 2021-06-01 → pre-reform regime
    assert project.decision_regime == "pre_2026_reform"
    assert project.project_type == "Aanvraag omgevingsproject"
    # Source displays aanvrager (naturals pre-anonymized server-side)
    assert project.applicant_name is not None
    assert "Natuurlijk perso(o)n(en)" in project.applicant_name
    assert project.description is not None
    assert project.description.startswith("Gevel-, dak-")
    assert project.case_language == "nl"
    # Honest address: projectnummer + authority, no invented street address
    assert "2021093523" in project.address.raw
    assert "Provincie Antwerpen" in project.address.raw
    assert project.address.street is None
    # Raw feature persisted as JSON next to the other sources' raw HTML
    assert project.raw_html_path.exists()
    persisted = json.loads(project.raw_html_path.read_text(encoding="utf-8"))
    assert persisted["properties"]["projectnummer"] == "2021093523"
    assert inquiry is None


@pytest.mark.asyncio
async def test_detail_municipality_and_intake_status(tmp_path: Path) -> None:
    """Municipal first-instance dossier: municipality set, INTAKE, post-reform."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=3)
            # Third fixture feature: 2021091909 — Oostkamp, eerste aanleg
            project, _ = await src.detail_pass("2021091909")

    assert "2021091909" in ids
    assert project.address.municipality == "Oostkamp"
    assert project.status == PermitProjectStatus.INTAKE
    # datum_indiening 2026-06-09 → post-reform regime
    assert project.decision_regime == "post_2026_reform"


# ---------------------------------------------------------------------------
# 5. vvo → municipality helper
# ---------------------------------------------------------------------------


def test_municipality_from_vvo_rejects_non_municipal_authorities() -> None:
    assert _municipality_from_vvo("Herentals") == "Herentals"
    assert _municipality_from_vvo("Oostkamp") == "Oostkamp"
    assert _municipality_from_vvo("Provincie Antwerpen") is None
    assert _municipality_from_vvo("Provincie West-Vlaanderen") is None
    assert _municipality_from_vvo("Vlaamse Overheid") is None
    assert _municipality_from_vvo("De Vlaamse Regering") is None
    assert _municipality_from_vvo(None) is None
    assert _municipality_from_vvo("") is None


def test_status_mapping_table() -> None:
    assert (
        _status_from_toestand("In behandeling (in eerste aanleg)")
        == PermitProjectStatus.INTAKE
    )
    assert (
        _status_from_toestand("In behandeling (na beroep)")
        == PermitProjectStatus.APPEALED
    )
    assert _status_from_toestand("Vergund") == PermitProjectStatus.DECIDED
    assert _status_from_toestand("Geweigerd") == PermitProjectStatus.DECIDED
    assert _status_from_toestand("Ingetrokken") == PermitProjectStatus.CLOSED
    assert _status_from_toestand(None) == PermitProjectStatus.INTAKE
    assert _status_from_toestand("Iets onbekends") == PermitProjectStatus.INTAKE


# ---------------------------------------------------------------------------
# 6. Lambert-72 → WGS84
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lambert72_multipolygon_centroid_in_flanders(tmp_path: Path) -> None:
    """Real EPSG:31370 MultiPolygon converts to a WGS84 point inside Belgium."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=1)
            project, _ = await src.detail_pass(ids[0])

    point = project.address.point
    assert point is not None, "Lambert-72 centroid conversion produced no point"
    assert point.crs == "EPSG:4326"
    # Feature 2021093523 lies in provincie Antwerpen (x≈191k, y≈224k Lambert)
    assert 50.6 <= point.lat <= 51.6
    assert 2.5 <= point.lon <= 6.4


# ---------------------------------------------------------------------------
# 7. content_hash idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_hash_stable_and_change_sensitive(tmp_path: Path) -> None:
    """Same feature → identical hash across runs; changed property → new hash."""
    settings = _settings(tmp_path)

    async def _hash_for(page: dict) -> str:
        with respx.mock(assert_all_called=False) as mock:
            _mock_gd(mock, [page])
            _mock_iioa(mock)
            async with MercatorSource(settings) as src:
                ids = await _collect_index(src, limit=1)
                project, _ = await src.detail_pass(ids[0])
        return project.content_hash

    h1 = await _hash_for(_GD_PAGE1)
    h2 = await _hash_for(_GD_PAGE1)
    assert h1 == h2, "content_hash must be deterministic for an unchanged dossier"

    mutated = json.loads(json.dumps(_GD_PAGE1))
    mutated["features"][0]["properties"]["datum_huidige_toestand"] = "2026-06-13"
    h3 = await _hash_for(mutated)
    assert h3 != h1, "content_hash must change when the dossier state changes"


# ---------------------------------------------------------------------------
# 8. IIOA join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iioa_join_sets_iioa_class(tmp_path: Path) -> None:
    """klasse_iioa from lu_omv_iioa_v2 lands on the matching gd project."""
    settings = _settings(tmp_path)

    # Remap the real IIOA fixture onto the first gd projectnummer
    iioa_payload = json.loads(json.dumps(_IIOA_JOIN))
    iioa_payload["features"][0]["properties"]["projectnummer"] = "2021093523"

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1])
        iioa_route = _mock_iioa(mock, iioa_payload)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=3)
            project, _ = await src.detail_pass(ids[0])
            other, _ = await src.detail_pass(ids[1])

    assert project.iioa_class == 1  # klasse_iioa from the real IIOA fixture
    assert other.iioa_class is None
    # One join request per page, filtering on the page's projectnummers
    assert iioa_route.call_count == 1
    cql = dict(iioa_route.calls[0].request.url.params)["CQL_FILTER"]
    assert cql.startswith("projectnummer IN (")
    assert "'2021093523'" in cql


@pytest.mark.asyncio
async def test_iioa_join_disabled_via_settings(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mercator_iioa_join=False)

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1])
        iioa_route = _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=1)
            project, _ = await src.detail_pass(ids[0])

    assert iioa_route.call_count == 0
    assert project.iioa_class is None


# ---------------------------------------------------------------------------
# 9. KNOWN GAP lock — no inquiry data in this source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inquiry_always_none_known_gap(tmp_path: Path) -> None:
    """Mercator exposes no openbaar-onderzoek state or inquiry dates.

    Locks the documented gap: if this ever fails, the source grew inquiry
    fields and the module docstring + status mapping must be revisited.
    """
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd(mock, [_GD_PAGE1])
        _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            ids = await _collect_index(src, limit=3)
            for nr in ids:
                project, inquiry = await src.detail_pass(nr)
                assert inquiry is None
                assert project.status != PermitProjectStatus.IN_PUBLIC_INQUIRY


# ---------------------------------------------------------------------------
# 10. Identified User-Agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identified_user_agent_on_every_request(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        gd_route = _mock_gd(mock, [_GD_PAGE1])
        iioa_route = _mock_iioa(mock)
        async with MercatorSource(settings) as src:
            await _collect_index(src, limit=3)

    for route in (gd_route, iioa_route):
        for call in route.calls:
            assert (
                call.request.headers["User-Agent"]
                == settings.nominatim_user_agent
            )


# ---------------------------------------------------------------------------
# 11. Registry + pipeline e2e
# ---------------------------------------------------------------------------


def test_mercator_source_in_registry() -> None:
    from debouw.pipeline import _SOURCE_REGISTRY

    assert "vlaanderen_mercator" in _SOURCE_REGISTRY
    assert _SOURCE_REGISTRY["vlaanderen_mercator"] is MercatorSource


@contextmanager
def _patch_settings(settings: Settings):
    with patch("debouw.pipeline.Settings", return_value=settings):
        yield


def _init_db(settings: Settings) -> None:
    from sqlalchemy import create_engine

    from debouw.storage.schema import Base

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{settings.db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()


_EMPTY_OVERLAY_FC = '{"type": "FeatureCollection", "features": []}'


@pytest.mark.asyncio
async def test_pipeline_e2e_mercator_preserves_geometry_point(tmp_path: Path) -> None:
    """Full pipeline run lands a row with the Lambert-72 centroid preserved
    (NOT clobbered by Nominatim) and skips it on the second, unchanged run."""
    settings = _settings(tmp_path)
    _init_db(settings)

    def _mocks(mock: respx.MockRouter) -> None:
        _mock_gd(mock, [_GD_PAGE1, _GD_PAGE2, _EMPTY_FC])
        _mock_iioa(mock)
        mock.get(url__regex=r".*nominatim.*").mock(
            return_value=Response(200, json=[])
        )
        mock.get(
            url__regex=r".*(geo\.api\.vlaanderen|inspirepub|onroerenderfgoed).*"
        ).mock(return_value=Response(200, text=_EMPTY_OVERLAY_FC))

    from debouw.pipeline import run

    with respx.mock(assert_all_called=False) as mock, _patch_settings(settings):
        _mocks(mock)
        r1 = await run("vlaanderen_mercator", limit=1)

    assert r1.ingested == 1
    assert r1.circuit_open is False

    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{settings.db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT external_id, source, region, address FROM permit_projects"
            )
        ).fetchone()
    engine.dispose()

    assert row is not None
    assert row[0] == "mercator:2021093523"
    assert row[1] == "vlaanderen_mercator"
    assert row[2] == "vl"
    address = json.loads(row[3])
    assert address["point"] is not None, (
        "geometry-derived point must survive the pipeline (Nominatim returned "
        "no hit for the address-less raw string)"
    )
    assert 50.6 <= address["point"]["lat"] <= 51.6

    # Second run: identical payload → content_hash gate skips the dossier
    with respx.mock(assert_all_called=False) as mock, _patch_settings(settings):
        _mocks(mock)
        r2 = await run("vlaanderen_mercator", limit=1)

    assert r2.ingested == 0
