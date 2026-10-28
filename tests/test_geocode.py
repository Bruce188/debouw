"""
Tests for the Nominatim geocoder with 30-day file cache.
All HTTP is mocked via respx. No live network calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
import structlog
from httpx import Response

from debouw.config import Settings
from debouw.ingest.geocode import geocode, geocode_vlaanderen, geocode_with_fallback


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path,
        db_path=tmp_path / "test.sqlite",
    )


NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
SEARCH_PATH = "/search"


# ---------------------------------------------------------------------------
# 7.4-1: Belgium hit returns GeoPoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_geocode_belgium_hit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    # Brussels coords (inside Belgium bbox)
    payload = [{"lat": "50.8503", "lon": "4.3517", "display_name": "Brussel, Belgium"}]

    with respx.mock(base_url=NOMINATIM_BASE) as mock:
        mock.get(SEARCH_PATH).mock(return_value=Response(200, json=payload))
        result = await geocode("Grote Markt 1, 1000 Brussel", settings)

    assert result is not None
    assert 50.0 < result.lat < 51.6
    assert 3.5 < result.lon < 6.4


# ---------------------------------------------------------------------------
# 7.4-2: Cache hit makes zero HTTP calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_geocode_cache_hit_makes_no_http_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = [{"lat": "51.0543", "lon": "3.7174", "display_name": "Gent, Belgium"}]

    call_count = 0

    with respx.mock(base_url=NOMINATIM_BASE) as mock:
        def _once(request):
            nonlocal call_count
            call_count += 1
            return Response(200, json=payload)

        mock.get(SEARCH_PATH).mock(side_effect=_once)

        # First call — hits network
        r1 = await geocode("Korenmarkt 1, 9000 Gent", settings)
        # Second call — should use cache
        r2 = await geocode("Korenmarkt 1, 9000 Gent", settings)

    assert r1 is not None
    assert r2 is not None
    assert r1.lat == r2.lat
    assert r1.lon == r2.lon
    assert call_count == 1, f"Expected 1 HTTP call, got {call_count}"


# ---------------------------------------------------------------------------
# 7.4-3: Coords outside Belgium bbox return None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_geocode_outside_bbox_returns_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    # Paris coords — outside Belgium
    payload = [{"lat": "48.8566", "lon": "2.3522", "display_name": "Paris, France"}]

    with respx.mock(base_url=NOMINATIM_BASE) as mock:
        mock.get(SEARCH_PATH).mock(return_value=Response(200, json=payload))
        result = await geocode("Tour Eiffel, Paris", settings)

    assert result is None


# ---------------------------------------------------------------------------
# 7.4-4: Empty address short-circuits (no HTTP call)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_geocode_empty_address_short_circuits(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with respx.mock(base_url=NOMINATIM_BASE, assert_all_called=False) as mock:
        mock.get(SEARCH_PATH).mock(return_value=Response(200, json=[]))
        result = await geocode("", settings)

    assert result is None
    # The mock should NOT have been called
    assert not mock.calls


# ---------------------------------------------------------------------------
# 7.4-5: Default UA triggers structlog warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_geocode_writes_warning_on_default_ua(tmp_path: Path) -> None:
    settings = Settings(
        data_root=tmp_path,
        db_path=tmp_path / "test.sqlite",
        nominatim_user_agent="debouw-research/0.x (set NOMINATIM_USER_AGENT in .env)",
    )
    payload = [{"lat": "51.0543", "lon": "3.7174", "display_name": "Gent"}]

    with respx.mock(base_url=NOMINATIM_BASE) as mock:
        mock.get(SEARCH_PATH).mock(return_value=Response(200, json=payload))
        with structlog.testing.capture_logs() as logs:
            await geocode("Gent", settings)

    events = [l["event"] for l in logs]
    assert "nominatim_default_ua" in events


# ---------------------------------------------------------------------------
# NEW TESTS — Vlaanderen geolocation API (geocode_vlaanderen)
# ---------------------------------------------------------------------------

GEOPUNT_BASE = "https://geo.api.vlaanderen.be"
GEOLOCATION_PATH = "/geolocation/v4/Location"

# Realistic v4 payload shape (Gent, Duddegemstraat 29)
_VL_HIT_PAYLOAD = {
    "LocationResult": [
        {
            "Municipality": "Gent",
            "Zipcode": "9051",
            "Thoroughfarename": "Duddegemstraat",
            "Housenumber": "29",
            "FormattedAddress": "Duddegemstraat 29, 9051 Gent",
            "Location": {
                "Lat_WGS84": 51.0238,
                "Lon_WGS84": 3.6587,
                "X_Lambert72": 100123.45,
                "Y_Lambert72": 192456.78,
            },
            "LocationType": "crab_huisnummer_manueleAanduidingVanPerceel",
        }
    ]
}


@pytest.mark.asyncio
async def test_geocode_vlaanderen_hit(tmp_path: Path) -> None:
    """First LocationResult's WGS84 coords become the GeoPoint."""
    settings = _settings(tmp_path)

    with respx.mock(base_url=GEOPUNT_BASE) as mock:
        mock.get(GEOLOCATION_PATH).mock(
            return_value=Response(200, json=_VL_HIT_PAYLOAD)
        )
        result = await geocode_vlaanderen("Duddegemstraat 29, 9051 Gent", settings)

    assert result is not None
    assert result.lat == pytest.approx(51.0238)
    assert result.lon == pytest.approx(3.6587)
    assert result.crs == "EPSG:4326"


@pytest.mark.asyncio
async def test_geocode_vlaanderen_empty_result(tmp_path: Path) -> None:
    """Empty LocationResult returns None."""
    settings = _settings(tmp_path)

    with respx.mock(base_url=GEOPUNT_BASE) as mock:
        mock.get(GEOLOCATION_PATH).mock(
            return_value=Response(200, json={"LocationResult": []})
        )
        result = await geocode_vlaanderen("Nergensstraat 1, 9000 Gent", settings)

    assert result is None


@pytest.mark.asyncio
async def test_geocode_vlaanderen_empty_address_short_circuits(tmp_path: Path) -> None:
    """Empty address makes no HTTP call."""
    settings = _settings(tmp_path)

    with respx.mock(base_url=GEOPUNT_BASE, assert_all_called=False) as mock:
        mock.get(GEOLOCATION_PATH).mock(
            return_value=Response(200, json={"LocationResult": []})
        )
        result = await geocode_vlaanderen("   ", settings)

    assert result is None
    assert not mock.calls


@pytest.mark.asyncio
async def test_geocode_vlaanderen_cache_hit_makes_no_second_call(tmp_path: Path) -> None:
    """Second identical query is served from the file cache."""
    settings = _settings(tmp_path)
    call_count = 0

    with respx.mock(base_url=GEOPUNT_BASE) as mock:
        def _once(request):
            nonlocal call_count
            call_count += 1
            return Response(200, json=_VL_HIT_PAYLOAD)

        mock.get(GEOLOCATION_PATH).mock(side_effect=_once)

        r1 = await geocode_vlaanderen("Duddegemstraat 29, 9051 Gent", settings)
        r2 = await geocode_vlaanderen("Duddegemstraat 29, 9051 Gent", settings)

    assert r1 is not None and r2 is not None
    assert (r1.lat, r1.lon) == (r2.lat, r2.lon)
    assert call_count == 1, f"Expected 1 HTTP call, got {call_count}"


@pytest.mark.asyncio
async def test_geocode_with_fallback_uses_nominatim_on_miss(tmp_path: Path) -> None:
    """Vlaanderen miss falls back to Nominatim."""
    settings = _settings(tmp_path)
    nominatim_payload = [
        {"lat": "51.0543", "lon": "3.7174", "display_name": "Gent, Belgium"}
    ]

    with respx.mock() as mock:
        mock.get(f"{GEOPUNT_BASE}{GEOLOCATION_PATH}").mock(
            return_value=Response(200, json={"LocationResult": []})
        )
        mock.get(f"{NOMINATIM_BASE}{SEARCH_PATH}").mock(
            return_value=Response(200, json=nominatim_payload)
        )
        result = await geocode_with_fallback("Korenmarkt 1, 9000 Gent", settings)

    assert result is not None
    assert result.lat == pytest.approx(51.0543)
    assert result.lon == pytest.approx(3.7174)


@pytest.mark.asyncio
async def test_geocode_with_fallback_prefers_vlaanderen(tmp_path: Path) -> None:
    """Vlaanderen hit short-circuits — Nominatim is never called."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GEOPUNT_BASE}{GEOLOCATION_PATH}").mock(
            return_value=Response(200, json=_VL_HIT_PAYLOAD)
        )
        nominatim_route = mock.get(f"{NOMINATIM_BASE}{SEARCH_PATH}").mock(
            return_value=Response(200, json=[])
        )
        result = await geocode_with_fallback("Duddegemstraat 29, 9051 Gent", settings)

    assert result is not None
    assert result.lat == pytest.approx(51.0238)
    assert not nominatim_route.called
