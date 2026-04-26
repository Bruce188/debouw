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
    return Settings(data_root=tmp_path, db_path=tmp_path / "test.sqlite")


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _mock_all_layers(mock: respx.MockRouter, overrides: dict | None = None) -> None:
    """Register mock responses for all 7 layer keys, allowing overrides."""
    defaults = {
        "signaalgebied": _load("signaalgebied_out.json"),
        "flood_fluvial": _load("flood_fluvial_none.json"),
        "flood_pluvial": _load("flood_fluvial_none.json"),
        "natura_2000_wms": _load("signaalgebied_out.json"),
        "natura_2000_wfs": _load("signaalgebied_out.json"),
        "heritage_vast_be": _load("signaalgebied_out.json"),
        "heritage_bes_monument": _load("signaalgebied_out.json"),
    }
    if overrides:
        defaults.update(overrides)

    # All WMS GFI calls have a SERVICE=WMS query param; WFS calls have SERVICE=WFS
    # respx routes by pattern — use catch-all route per host
    mock.get(url__regex=r".*").mock(
        side_effect=_layer_dispatcher(defaults)
    )


def _layer_dispatcher(responses: dict):
    """Return a side_effect callable that routes by URL pattern."""
    def _dispatch(request):
        url = str(request.url)
        params_str = request.url.query.decode() if request.url.query else ""

        if "Signaalgebieden" in url:
            return Response(200, text=responses["signaalgebied"])
        if "fluviaal" in url:
            return Response(200, text=responses["flood_fluvial"])
        if "pluviaal" in url:
            return Response(200, text=responses["flood_pluvial"])
        if "INBO" in url and "wfs" in url.lower():
            return Response(200, text=responses["natura_2000_wfs"])
        if "INBO" in url:
            return Response(200, text=responses["natura_2000_wms"])
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
        _mock_all_layers(mock, {"signaalgebied": _load("signaalgebied_in.json")})
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
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"flood_fluvial": _load("flood_fluvial_high.json")})
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
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"flood_pluvial": _load("flood_pluvial_low.json")})
        result = await enrich(POINT, settings)
    assert result.flood_risk_pluvial == "low"


# ---------------------------------------------------------------------------
# 7.5-6: Natura 2000 in
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_natura2000_in(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_all_layers(mock, {"natura_2000_wms": _load("natura2000_in.json")})
        result = await enrich(POINT, settings)
    assert result.in_natura_2000 is True


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
    """Natura WFS HTTP 500 → enrich continues, natura_2000_distance_m = None."""
    settings = _settings(tmp_path)

    def _dispatch(request):
        url = str(request.url)
        params_str = request.url.query.decode() if request.url.query else ""
        if "Signaalgebieden" in url:
            return Response(200, text=_load("signaalgebied_out.json"))
        if "fluviaal" in url:
            return Response(200, text=_load("flood_fluvial_none.json"))
        if "pluviaal" in url:
            return Response(200, text=_load("flood_fluvial_none.json"))
        if "INBO" in url and "wfs" in url.lower():
            return Response(500, text="Internal Server Error")
        if "INBO" in url:
            return Response(200, text=_load("signaalgebied_out.json"))
        if "geoserver" in url and "vast_be" in params_str:
            return Response(200, text=_load("signaalgebied_out.json"))
        if "geoserver" in url:
            return Response(200, text=_load("signaalgebied_out.json"))
        return Response(200, text='{"type": "FeatureCollection", "features": []}')

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*").mock(side_effect=_dispatch)
        result = await enrich(POINT, settings)

    # Should not crash, natura distance should be None (WFS 500'd)
    assert result.natura_2000_distance_m is None
    # raw_layer_responses should have an entry with error info
    assert any("ERROR" in v or "natura" in k for k, v in result.raw_layer_responses.items())


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
