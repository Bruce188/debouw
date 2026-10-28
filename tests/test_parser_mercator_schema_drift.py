"""Schema-drift lock tests for MercatorSource.

Locks the WFS GetFeature contract (GeoJSON envelope + required properties)
against the real fixtures captured 2026-06-12. NO live network — all HTTP is
respx-mocked. Follows the test_parser_inzageloket_schema_drift.py pattern:
mutate the captured payload the way real drift would look, assert
SchemaDriftError.

If a test here breaks after a probe run, the MercatorNet layer contract
changed — revalidate lu:lu_omv_gd_v2 / lu:lu_omv_iioa_v2 before patching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from debouw.config import Settings
from debouw.ingest.sources.base import SchemaDriftError
from debouw.ingest.sources.mercator import MercatorSource

_FIXTURES = Path(__file__).parent / "fixtures" / "vlaanderen_mercator"
_GD_PAGE1_TEXT = (_FIXTURES / "gd_page1.json").read_text(encoding="utf-8")

_WFS_URL_RE = r".*mercator\.vlaanderen\.be.*/wfs.*"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        db_path=tmp_path / "data" / "debouw.sqlite",
        throttle_mercator_seconds=0.01,
        mercator_page_size=3,
        mercator_iioa_join=False,  # isolate the gd-layer contract
    )


def _mock_gd_text(mock: respx.MockRouter, body: str, status: int = 200):
    return mock.get(
        url__regex=_WFS_URL_RE,
        params__contains={"typeNames": "lu:lu_omv_gd_v2"},
    ).mock(return_value=Response(status, text=body, headers={"content-type": "application/json"}))


async def _drain_index(src: MercatorSource) -> list[str]:
    return [nr async for nr in src.index_pass(limit=None)]


@pytest.mark.asyncio
async def test_features_envelope_renamed_raises_drift(tmp_path: Path) -> None:
    """index_pass raises SchemaDriftError when the GeoJSON 'features' key drifts.

    Locks the FeatureCollection envelope: features → results would be the
    classic GeoServer→API-gateway migration symptom.
    """
    mutated = _GD_PAGE1_TEXT.replace('"features"', '"results"', 1)
    assert '"results"' in mutated, "Mutation did not take effect"

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd_text(mock, mutated)
        async with MercatorSource(_settings(tmp_path)) as src:
            with pytest.raises(SchemaDriftError):
                await _drain_index(src)


@pytest.mark.asyncio
async def test_projectnummer_property_renamed_raises_drift(tmp_path: Path) -> None:
    """index_pass raises SchemaDriftError when 'projectnummer' is renamed.

    projectnummer is the natural key (external_id = mercator:<projectnummer>);
    silent loss would corrupt idempotency, so drift must be loud.
    """
    mutated = _GD_PAGE1_TEXT.replace('"projectnummer"', '"dossiernummer"')
    assert '"projectnummer"' not in mutated, "Mutation did not take effect"

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd_text(mock, mutated)
        async with MercatorSource(_settings(tmp_path)) as src:
            with pytest.raises(SchemaDriftError):
                await _drain_index(src)


@pytest.mark.asyncio
async def test_huidige_toestand_renamed_raises_drift_on_detail(tmp_path: Path) -> None:
    """detail_pass raises SchemaDriftError when 'huidige_toestand' is renamed.

    Status mapping depends on this literal; INTAKE-by-default fallback would
    silently misclassify every dossier, so the rename must be loud instead.
    """
    mutated = _GD_PAGE1_TEXT.replace('"huidige_toestand"', '"toestand"')
    assert '"huidige_toestand"' not in mutated, "Mutation did not take effect"

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd_text(mock, mutated)
        async with MercatorSource(_settings(tmp_path)) as src:
            ids = []
            async for nr in src.index_pass(limit=1):
                ids.append(nr)
            with pytest.raises(SchemaDriftError):
                await src.detail_pass(ids[0])


@pytest.mark.asyncio
async def test_non_json_response_raises_drift(tmp_path: Path) -> None:
    """index_pass raises SchemaDriftError on a non-JSON (e.g. XML error) body."""
    xml_error = (
        '<?xml version="1.0"?><ows:ExceptionReport>'
        "<ows:Exception/></ows:ExceptionReport>"
    )

    with respx.mock(assert_all_called=False) as mock:
        _mock_gd_text(mock, xml_error)
        async with MercatorSource(_settings(tmp_path)) as src:
            with pytest.raises(SchemaDriftError):
                await _drain_index(src)


@pytest.mark.asyncio
async def test_unmutated_fixture_passes_lock(tmp_path: Path) -> None:
    """Control: the captured live payload parses without drift (3 dossiers)."""
    payload = json.loads(_GD_PAGE1_TEXT)
    payload_short = dict(payload)  # 3 features < page_size? equal — add empty page
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(
            url__regex=_WFS_URL_RE,
            params__contains={"typeNames": "lu:lu_omv_gd_v2"},
        )
        route.side_effect = [
            Response(200, json=payload_short),
            Response(
                200,
                json={"type": "FeatureCollection", "features": []},
            ),
        ]
        async with MercatorSource(_settings(tmp_path)) as src:
            ids = await _drain_index(src)
            projects = [await src.detail_pass(nr) for nr in ids]

    assert len(ids) == 3
    assert all(p.source == "vlaanderen_mercator" for p, _ in projects)
