"""
Tests for the Geopunt WMS/WFS enricher.
All HTTP is mocked via respx. No live network calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from debouw.config import Settings
from debouw.ingest.enrich_geopunt import enrich
from debouw.models.permit import GeoPoint

FIXTURES = Path(__file__).parent / "fixtures" / "geopunt_overlay_responses"

# Reference point inside Gent
POINT = GeoPoint(lat=51.0543, lon=3.7174)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path,
        db_path=tmp_path / "test.sqlite",
        # All HTTP is mocked — zero the per-source throttles for test speed.
        throttle_geopunt_seconds=0.0,
        throttle_waterinfo_seconds=0.0,
        throttle_mercator_seconds=0.0,
    )


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_all_layers(mock: respx.MockRouter, overrides: dict | None = None) -> None:
    """Register mock responses for all 8 layer keys, allowing overrides."""
    empty = _load("esri_flood_empty.json")
    defaults = {
        "signaalgebied": empty,
        "flood_fluvial": empty,
        "flood_pluvial": empty,
        "natura_hbtrl": empty,
        "natura_vglrl": empty,
        "natura_deel": empty,
        "heritage_vast_be": _load("signaalgebied_out.json"),
        "heritage_bes_monument": _load("signaalgebied_out.json"),
    }
    if overrides:
        defaults.update(overrides)

    # respx routes by pattern — use a catch-all route + URL-substring dispatch
    mock.get(url__regex=r".*").mock(
        side_effect=_layer_dispatcher(defaults)
    )


def _layer_dispatcher(responses: dict):
    """Return a side_effect callable that routes by URL pattern.

    Order matters: ps_hbtrl_deel must be checked before ps_hbtrl.
    """
    def _dispatch(request):
        url = str(request.url)
        params_str = request.url.query.decode() if request.url.query else ""

        if "Signaalgebieden" in url:
            return Response(200, text=responses["signaalgebied"])
        if "overstromingsgevoelige_gebieden_fluviaal" in url:
            return Response(200, text=responses["flood_fluvial"])
        if "overstromingsgevoelige_gebieden_pluviaal" in url:
            return Response(200, text=responses["flood_pluvial"])
        if "ps_hbtrl_deel" in url:
            return Response(200, text=responses["natura_deel"])
        if "ps_hbtrl" in url:
            return Response(200, text=responses["natura_hbtrl"])
        if "ps_vglrl" in url:
            return Response(200, text=responses["natura_vglrl"])
        if "geoserver" in url and "vast_be" in params_str:
            return Response(200, text=responses["heritage_vast_be"])
        if "geoserver" in url:
            return Response(200, text=responses["heritage_bes_monument"])
        return Response(200, text='{"type": "FeatureCollection", "features": []}')

    return _dispatch


# ---------------------------------------------------------------------------
# 7.5-1/2: Signaalgebied true/false
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signaalgebied_true(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"signaalgebied": _load("esri_signaalgebied_hit.json")})
        result = await enrich(POINT, settings)
    assert result.in_signaalgebied is True


@pytest.mark.asyncio
async def test_signaalgebied_false(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock)
        result = await enrich(POINT, settings)
    assert result.in_signaalgebied is False


# ---------------------------------------------------------------------------
# 7.5-3/4: Flood fluvial high / none
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flood_fluvial_high(tmp_path: Path) -> None:
    """Live-captured ESRI response: gridcode 3 maps to 'high'."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"flood_fluvial": _load("esri_flood_fluvial_hit.json")})
        result = await enrich(POINT, settings)
    assert result.flood_risk_fluvial == "high"


@pytest.mark.asyncio
async def test_flood_fluvial_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock)
        result = await enrich(POINT, settings)
    assert result.flood_risk_fluvial == "none"


# ---------------------------------------------------------------------------
# 7.5-5: Flood pluvial low
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flood_pluvial_low(tmp_path: Path) -> None:
    """Legacy pixelvalue/Klasse mapping stays supported alongside gridcode."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"flood_pluvial": _load("flood_pluvial_low.json")})
        result = await enrich(POINT, settings)
    assert result.flood_risk_pluvial == "low"


@pytest.mark.asyncio
async def test_flood_pluvial_gridcode_high(tmp_path: Path) -> None:
    """Live-captured pluvial ESRI response: gridcode 3 maps to 'high'."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"flood_pluvial": _load("esri_flood_pluvial_hit.json")})
        result = await enrich(POINT, settings)
    assert result.flood_risk_pluvial == "high"


# ---------------------------------------------------------------------------
# 7.5-6: Natura 2000 in (boolean via ps_hbtrl / ps_vglrl, distance via _deel)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natura2000_in(tmp_path: Path) -> None:
    """Habitat Directive (ps:ps_hbtrl) hit sets the boolean."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"natura_hbtrl": _load("natura2000_in.json")})
        result = await enrich(POINT, settings)
    assert result.in_natura_2000 is True


@pytest.mark.asyncio
async def test_natura2000_in_via_vglrl(tmp_path: Path) -> None:
    """Birds Directive (ps:ps_vglrl) hit alone also sets the boolean."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"natura_vglrl": _load("natura2000_in.json")})
        result = await enrich(POINT, settings)
    assert result.in_natura_2000 is True


@pytest.mark.asyncio
async def test_natura2000_distance_via_deel(tmp_path: Path) -> None:
    """Distance comes from the ps:ps_hbtrl_deel sub-polygons only."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"natura_deel": _load("natura2000_in.json")})
        result = await enrich(POINT, settings)
    # natura2000_in.json polygon first vertex is ~700 m from POINT
    assert result.natura_2000_distance_m is not None
    assert 0 < result.natura_2000_distance_m < 5000
    # boolean queries returned empty — distance alone must not flip it
    assert result.in_natura_2000 is False


# ---------------------------------------------------------------------------
# 7.5-7: Heritage in
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heritage_in(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"heritage_vast_be": _load("heritage_in.json")})
        result = await enrich(POINT, settings)
    assert result.in_protected_heritage is True


# ---------------------------------------------------------------------------
# 7.5-8: Heritage distance in meters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heritage_distance_meters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(
            mock,
            {
                "heritage_bes_monument": _load("heritage_distance.json"),
                "heritage_vast_be": _load("signaalgebied_out.json"),
            },
        )
        result = await enrich(POINT, settings)
    # heritage_distance.json has a point ~1.5 km away
    assert result.heritage_match_distance_m is not None
    assert 0 < result.heritage_match_distance_m < 5000


# ---------------------------------------------------------------------------
# 7.5-9: Layer failure falls soft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_layer_failure_falls_soft(tmp_path: Path) -> None:
    """Natura distance WFS HTTP 500 → enrich continues, natura_2000_distance_m = None."""
    settings = _settings(tmp_path)

    def _dispatch(request):
        url = str(request.url)
        if "ps_hbtrl_deel" in url:
            return Response(500, text="Internal Server Error")
        return Response(200, text='{"type": "FeatureCollection", "features": []}')

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").mock(side_effect=_dispatch)
        result = await enrich(POINT, settings)

    # Should not crash, natura distance should be None (WFS 500'd)
    assert result.natura_2000_distance_m is None
    # raw_layer_responses should have an entry with error info
    assert any("ERROR" in v for v in result.raw_layer_responses.values())
    assert "ERROR" in result.raw_layer_responses.get("natura_2000_deel", "")


# ---------------------------------------------------------------------------
# 7.5-10: Raw response truncated at 50 KB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raw_layer_responses_truncated(tmp_path: Path) -> None:
    """A 100 KB response should be stored as ≤50 KB."""
    settings = _settings(tmp_path)
    big_payload = '{"type": "FeatureCollection", "features": [], "extra": "' + ("a" * 100_000) + '"}'

    def _dispatch_big(request):
        url = str(request.url)
        if "Signaalgebieden" in url:
            return Response(200, text=big_payload)
        return Response(200, text='{"type": "FeatureCollection", "features": []}')

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").mock(side_effect=_dispatch_big)
        result = await enrich(POINT, settings)

    sig_raw = result.raw_layer_responses.get("signaalgebied", "")
    assert len(sig_raw) <= 50_000, f"Expected ≤50K, got {len(sig_raw)}"


# ---------------------------------------------------------------------------
# NEW TEST — WFS BBOX must carry an explicit CRS (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wfs_bbox_carries_explicit_crs(tmp_path: Path) -> None:
    """WFS GetFeature requests pin BBOX CRS + SRSNAME to WGS84.

    Without the CRS suffix GeoServer interprets the lat/lon-degree BBOX in
    the layer's native CRS (Lambert72 metres for the heritage layers) and
    silently matches nothing — every dossier then reads heritage-clean.
    """
    settings = _settings(tmp_path)
    wfs_requests = []

    def _dispatch(request):
        url = str(request.url)
        params = dict(request.url.params)
        if params.get("SERVICE") == "WFS":
            wfs_requests.append(params)
        return Response(200, text='{"type": "FeatureCollection", "features": []}')

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").mock(side_effect=_dispatch)
        await enrich(POINT, settings)

    assert wfs_requests, "Expected at least one WFS GetFeature call"
    for params in wfs_requests:
        assert params["BBOX"].endswith(",urn:ogc:def:crs:EPSG::4326"), (
            f"WFS BBOX lacks explicit CRS: {params['BBOX']!r}"
        )
        assert params.get("SRSNAME") == "urn:ogc:def:crs:EPSG::4326"


# ---------------------------------------------------------------------------
# Heritage boolean BBOX must stay parcel-scale (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heritage_boolean_bbox_is_parcel_scale(tmp_path: Path) -> None:
    """The vast_be presence check must use a parcel-scale BBOX (≤ ~±55 m).

    Regression: the boolean layer inherited the wide distance-discovery
    default (delta=0.05 ≈ ±5.5 km), so in_protected_heritage flipped true
    for every address in Flanders — any inventoried heritage within ~5.5 km
    counted as a direct hit.
    """
    settings = _settings(tmp_path)
    vast_bboxes = []

    def _dispatch(request):
        params = dict(request.url.params)
        if params.get("TYPENAMES") == "vioe_geoportaal:vast_be":
            vast_bboxes.append(params["BBOX"])
        return Response(200, text='{"type": "FeatureCollection", "features": []}')

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").mock(side_effect=_dispatch)
        await enrich(POINT, settings)

    assert vast_bboxes, "Expected a vast_be WFS GetFeature call"
    for bbox in vast_bboxes:
        lat_min, lon_min, lat_max, lon_max = (float(p) for p in bbox.split(",")[:4])
        half_width = (lat_max - lat_min) / 2
        assert half_width <= 0.001, (
            f"vast_be boolean BBOX half-width {half_width} deg (~"
            f"{half_width * 111_000:.0f} m) — boolean check must stay "
            f"parcel-scale, not distance-discovery scale"
        )


# ---------------------------------------------------------------------------
# WFS disk-cache key must encode the BBOX delta (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wfs_cache_key_encodes_bbox_delta(tmp_path: Path) -> None:
    """WFS cache filenames carry the query delta.

    Regression: the cache was keyed (layer, lat, lon) only, so after the
    heritage BBOX shrank from 0.05 to 0.0005 the enrichment kept serving
    the pre-fix wide-box responses from disk — reparse-gent reported
    "overlays changed: (none)" while the live endpoint disagreed.
    """
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").mock(
            return_value=Response(
                200, text='{"type": "FeatureCollection", "features": []}'
            )
        )
        await enrich(POINT, settings)

    cache_dir = settings.data_root / "cache" / "geopunt"
    wfs_files = [p.name for p in cache_dir.glob("heritage_vast_be_*.json")]
    assert wfs_files, "Expected a cached vast_be response"
    for name in wfs_files:
        assert "_d0.0005" in name, (
            f"WFS cache file {name!r} lacks the BBOX-delta qualifier — "
            f"a delta change would silently reuse stale responses"
        )
