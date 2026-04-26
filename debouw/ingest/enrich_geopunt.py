"""
Geopunt WMS/WFS enricher — populates GeoOverlays for a geocoded project.

Four enrichment calls (all fail-soft):
  1. Signaalgebied (WMS GFI)
  2. Overstroming fluviaal + pluviaal (WMS GFI)
  3. Natura 2000 (WMS GFI boolean + WFS distance)
  4. Heritage (WFS GetFeature — vast_be + bes_monument)

30-day file cache identical to geocode.py.
Raw responses stored in GeoOverlays.raw_layer_responses (≤50 KB each).
Total LoC ≤ 250.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Literal

import structlog

from debouw.config import Settings
from debouw.ingest.http import create_http_client
from debouw.models.permit import GeoOverlays, GeoPoint

log = structlog.get_logger(__name__)

_MAX_RAW_BYTES = 50_000  # truncate raw layer responses to 50 KB
_CACHE_TTL_DAYS = 30
FloodLevel = Literal["none", "low", "medium", "high"]


# ---------------------------------------------------------------------------
# Cache helpers (mirrors geocode.py)
# ---------------------------------------------------------------------------

def _cache_path(layer: str, lat: float, lon: float, settings: Settings) -> Path:
    return settings.data_root / "cache" / "geopunt" / f"{layer}_{lat:.5f}_{lon:.5f}.json"


def _load_cache(path: Path, ttl_days: int = _CACHE_TTL_DAYS) -> dict | None:
    if not path.exists():
        return None
    age_days = (time.time() - path.stat().st_mtime) / 86400
    if age_days > ttl_days:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Distance helper
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two WGS84 points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# WMS GetFeatureInfo helper
# ---------------------------------------------------------------------------

def _bbox_around(point: GeoPoint, delta: float = 0.001) -> str:
    lat, lon = point.lat, point.lon
    return f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}"


def _gfi_params(layer: str, point: GeoPoint) -> dict:
    return {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetFeatureInfo",
        "LAYERS": layer,
        "QUERY_LAYERS": layer,
        "STYLES": "",
        "BBOX": _bbox_around(point),
        "WIDTH": 1,
        "HEIGHT": 1,
        "X": 0,
        "Y": 0,
        "SRS": "EPSG:4326",
        "INFO_FORMAT": "application/json",
    }


def _truncate(raw: str) -> str:
    return raw[:_MAX_RAW_BYTES]


def _has_features(data: dict) -> bool:
    """Return True if a GeoJSON FeatureCollection has at least one feature."""
    return bool(data.get("features"))


def _flood_class(data: dict) -> FloodLevel:
    """Map ArcGIS pixelvalue/Klasse to flood severity."""
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        pv = props.get("pixelvalue") or props.get("Klasse") or props.get("klasse")
        if pv is None:
            continue
        pv_str = str(pv).lower()
        if pv_str in ("1", "weinig"):
            return "low"
        if pv_str in ("2", "matig"):
            return "medium"
        if pv_str in ("3", "groot"):
            return "high"
    return "none"


def _nearest_centroid_m(data: dict, lat: float, lon: float) -> float | None:
    """Find the nearest feature centroid and return distance in metres."""
    min_dist: float | None = None
    for feat in data.get("features", []):
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates")
        if not coords:
            continue
        gtype = geom.get("type", "")
        if gtype == "Point":
            flon, flat = coords[0], coords[1]
        elif gtype in ("Polygon", "MultiPolygon"):
            # Use the first vertex as a rough centroid approximation
            try:
                if gtype == "Polygon":
                    flon, flat = coords[0][0]
                else:
                    flon, flat = coords[0][0][0]
            except (IndexError, TypeError):
                continue
        else:
            continue
        d = _haversine_m(lat, lon, flat, flon)
        if min_dist is None or d < min_dist:
            min_dist = d
    return min_dist


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

async def enrich(point: GeoPoint | None, settings: Settings) -> GeoOverlays:
    """Enrich a geocoded point with Geopunt spatial overlays.

    Returns GeoOverlays with all defaults when point is None.
    All layer calls fail-soft: exceptions log a warning and continue.
    """
    if point is None:
        return GeoOverlays()

    lat, lon = point.lat, point.lon
    raw: dict[str, str] = {}

    # Convenience: fire a WMS GFI call and return parsed JSON (or {} on error)
    async def _gfi(client, url: str, layer: str, layer_key: str) -> dict:
        cache_key = f"{layer_key}_{lat:.5f}_{lon:.5f}"
        cpath = _cache_path(layer_key, lat, lon, settings)
        cached = _load_cache(cpath)
        if cached is not None:
            raw[layer_key] = _truncate(json.dumps(cached))
            return cached
        try:
            resp = await client.get(url, params=_gfi_params(layer, point))
            text = resp.text
            raw[layer_key] = _truncate(text)
            data = json.loads(text)
            _save_cache(cpath, data)
            return data
        except Exception as exc:
            log.warning("geopunt_layer_failed", layer=layer_key, error=str(exc))
            raw[layer_key] = f"ERROR: {exc}"
            return {}

    async def _wfs(client, url: str, layer_key: str, type_name: str,
                   lat_: float, lon_: float, delta: float = 0.05) -> dict:
        cpath = _cache_path(layer_key, lat_, lon_, settings)
        cached = _load_cache(cpath)
        if cached is not None:
            raw[layer_key] = _truncate(json.dumps(cached))
            return cached
        params = {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": type_name, "OUTPUTFORMAT": "application/json",
            "BBOX": f"{lat_ - delta},{lon_ - delta},{lat_ + delta},{lon_ + delta}",
        }
        try:
            resp = await client.get(url, params=params)
            text = resp.text
            raw[layer_key] = _truncate(text)
            data = json.loads(text)
            _save_cache(cpath, data)
            return data
        except Exception as exc:
            log.warning("geopunt_wfs_failed", layer=layer_key, error=str(exc))
            raw[layer_key] = f"ERROR: {exc}"
            return {}

    # --- 1. Signaalgebied (WMS GFI) ---
    in_signaalgebied = False
    try:
        client_wp = create_http_client(settings, source="geopunt")
        sig_url = "/arcgis/services/Signaalgebieden/MapServer/WMSServer"
        sig_data = await _gfi(client_wp, sig_url, "Signaalgebieden", "signaalgebied")
        in_signaalgebied = _has_features(sig_data)
        await client_wp.aclose()
    except Exception as exc:
        log.warning("signaalgebied_failed", error=str(exc))

    # --- 2. Overstroming fluviaal + pluviaal (WMS GFI) ---
    flood_fluvial: FloodLevel = "none"
    flood_pluvial: FloodLevel = "none"
    try:
        client_winfo = create_http_client(settings, source="geopunt")
        # Use inspirepub_waterinfo_base for flood layers
        fluvial_url = "/arcgis/services/informatieplicht/overstromingsgevoelige_gebieden_fluviaal/MapServer/WMSServer"
        fluvial_data = await _gfi(client_winfo, fluvial_url,
                                   "overstromingsgevoelige_gebieden_fluviaal",
                                   "flood_fluvial")
        flood_fluvial = _flood_class(fluvial_data)

        pluvial_url = "/arcgis/services/informatieplicht/overstromingsgevoelige_gebieden_pluviaal/MapServer/WMSServer"
        pluvial_data = await _gfi(client_winfo, pluvial_url,
                                   "overstromingsgevoelige_gebieden_pluviaal",
                                   "flood_pluvial")
        flood_pluvial = _flood_class(pluvial_data)
        await client_winfo.aclose()
    except Exception as exc:
        log.warning("flood_layers_failed", error=str(exc))

    # --- 3. Natura 2000 (WMS GFI boolean + WFS distance) ---
    in_natura_2000 = False
    natura_2000_distance_m: float | None = None
    try:
        client_gp = create_http_client(settings, source="geopunt")
        n2k_gfi_url = "/INBO/wms"
        n2k_data = await _gfi(client_gp, n2k_gfi_url, "BWK2Hab", "natura_2000_wms")
        in_natura_2000 = _has_features(n2k_data)

        n2k_wfs_url = "/INBO/wfs"
        n2k_wfs = await _wfs(client_gp, n2k_wfs_url, "natura_2000_wfs", "BWK2Hab", lat, lon)
        natura_2000_distance_m = _nearest_centroid_m(n2k_wfs, lat, lon)
        await client_gp.aclose()
    except Exception as exc:
        log.warning("natura_2000_failed", error=str(exc))

    # --- 4. Heritage (WFS GetFeature) ---
    in_protected_heritage = False
    heritage_match_distance_m: float | None = None
    try:
        client_oe = create_http_client(settings, source="onroerend_erfgoed")
        vast_url = "/geoserver/wfs"
        vast_data = await _wfs(client_oe, vast_url, "heritage_vast_be",
                               "vioe_geoportaal:vast_be", lat, lon)
        in_protected_heritage = _has_features(vast_data)

        bes_data = await _wfs(client_oe, vast_url, "heritage_bes_monument",
                              "vioe_geoportaal:bes_monument", lat, lon)
        bes_dist = _nearest_centroid_m(bes_data, lat, lon)
        if bes_dist is not None:
            heritage_match_distance_m = bes_dist
        elif in_protected_heritage:
            vast_dist = _nearest_centroid_m(vast_data, lat, lon)
            heritage_match_distance_m = vast_dist
        await client_oe.aclose()
    except Exception as exc:
        log.warning("heritage_failed", error=str(exc))

    return GeoOverlays(
        in_natura_2000=in_natura_2000,
        natura_2000_distance_m=natura_2000_distance_m,
        in_signaalgebied=in_signaalgebied,
        flood_risk_fluvial=flood_fluvial,
        flood_risk_pluvial=flood_pluvial,
        in_protected_heritage=in_protected_heritage,
        heritage_match_distance_m=heritage_match_distance_m,
        raw_layer_responses=raw,
    )
