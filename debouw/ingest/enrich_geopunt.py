"""
Spatial overlay enricher — populates GeoOverlays for a geocoded project.

Four enrichment calls (all fail-soft):
  1. Signaalgebied (waterinfo ArcGIS REST point query)
  2. Overstroming fluviaal + pluviaal (waterinfo ArcGIS REST point queries,
     gridcode legend: 0=none, 1=low, 2=medium, 3=high)
  3. Natura 2000 (Mercator WFS: ps_hbtrl/ps_vglrl boolean +
     ps_hbtrl_deel sub-polygon distance)
  4. Heritage (onroerend erfgoed WFS GetFeature — vast_be + bes_monument)

30-day file cache identical to geocode.py.
Raw responses stored in GeoOverlays.raw_layer_responses (≤50 KB each).
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

def _cache_path(
    layer: str, lat: float, lon: float, settings: Settings, qualifier: str = ""
) -> Path:
    """Disk-cache path for one layer query at one point.

    ``qualifier`` must encode any query parameter that changes the response
    (e.g. the WFS BBOX delta) — otherwise a code change to the query silently
    keeps serving pre-change cached responses.
    """
    suffix = f"_{qualifier}" if qualifier else ""
    return (
        settings.data_root / "cache" / "geopunt"
        / f"{layer}_{lat:.5f}_{lon:.5f}{suffix}.json"
    )


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
# ArcGIS REST point-query helper
# ---------------------------------------------------------------------------

def _esri_params(point: GeoPoint, out_fields: str) -> dict:
    """Params for an ArcGIS REST point-intersection query returning GeoJSON."""
    return {
        "f": "geojson",
        "geometry": f"{point.lon},{point.lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnGeometry": "false",
        "outFields": out_fields,
    }


def _truncate(raw: str) -> str:
    return raw[:_MAX_RAW_BYTES]


def _has_features(data: dict) -> bool:
    """Return True if a GeoJSON FeatureCollection has at least one feature."""
    return bool(data.get("features"))


def _flood_class(data: dict) -> FloodLevel:
    """Map ArcGIS pixelvalue/Klasse/gridcode to flood severity."""
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        pv = None
        for key in ("pixelvalue", "Klasse", "klasse", "gridcode"):
            if props.get(key) is not None:
                pv = props[key]
                break
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

    # Convenience: fire an ArcGIS REST point query and return parsed GeoJSON
    # (or {} on error) — same cache/raw/fail-soft pattern as _wfs below.
    async def _esri(client, url: str, layer_key: str, out_fields: str) -> dict:
        cpath = _cache_path(layer_key, lat, lon, settings)
        cached = _load_cache(cpath)
        if cached is not None:
            raw[layer_key] = _truncate(json.dumps(cached))
            return cached
        try:
            resp = await client.get(url, params=_esri_params(point, out_fields))
            text = resp.text
            raw[layer_key] = _truncate(text)
            data = json.loads(text)
            _save_cache(cpath, data)
            return data
        except Exception as exc:
            log.warning("geopunt_layer_failed", layer=layer_key, error=repr(exc))
            raw[layer_key] = f"ERROR: {exc!r}"
            return {}

    async def _wfs(client, url: str, layer_key: str, type_name: str,
                   lat_: float, lon_: float, delta: float = 0.05) -> dict:
        cpath = _cache_path(layer_key, lat_, lon_, settings, qualifier=f"d{delta}")
        cached = _load_cache(cpath)
        if cached is not None:
            raw[layer_key] = _truncate(json.dumps(cached))
            return cached
        # BBOX must carry an explicit CRS: without it GeoServer interprets the
        # lat/lon-degree box in the layer's native CRS (Lambert72 metres for
        # the heritage layers), which matches nothing — every dossier then
        # silently reads as heritage-clean. SRSNAME forces the response
        # geometry back to WGS84 (GeoJSON lon,lat) so the haversine distance
        # in _nearest_centroid_m stays meaningful.
        _crs = "urn:ogc:def:crs:EPSG::4326"
        params = {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": type_name, "OUTPUTFORMAT": "application/json",
            "BBOX": f"{lat_ - delta},{lon_ - delta},{lat_ + delta},{lon_ + delta},{_crs}",
            "SRSNAME": _crs,
        }
        try:
            resp = await client.get(url, params=params)
            text = resp.text
            raw[layer_key] = _truncate(text)
            data = json.loads(text)
            _save_cache(cpath, data)
            return data
        except Exception as exc:
            log.warning("geopunt_wfs_failed", layer=layer_key, error=repr(exc))
            raw[layer_key] = f"ERROR: {exc!r}"
            return {}

    # --- 1+2. Signaalgebied + overstroming fluviaal/pluviaal
    #          (waterinfo ArcGIS REST point queries, shared throttled client) ---
    in_signaalgebied = False
    flood_fluvial: FloodLevel = "none"
    flood_pluvial: FloodLevel = "none"
    try:
        client_wi = create_http_client(settings, source="waterinfo")
        sig_data = await _esri(
            client_wi,
            "/arcgis/rest/services/Signaalgebieden/MapServer/0/query",
            "signaalgebied",
            "ID_SG,Naam_SG,Gemeente",
        )
        in_signaalgebied = _has_features(sig_data)

        fluvial_data = await _esri(
            client_wi,
            "/arcgis/rest/services/informatieplicht/overstromingsgevoelige_gebieden_fluviaal/MapServer/0/query",
            "flood_fluvial",
            "gridcode",
        )
        flood_fluvial = _flood_class(fluvial_data)

        pluvial_data = await _esri(
            client_wi,
            "/arcgis/rest/services/informatieplicht/overstromingsgevoelige_gebieden_pluviaal/MapServer/0/query",
            "flood_pluvial",
            "gridcode",
        )
        flood_pluvial = _flood_class(pluvial_data)
        await client_wi.aclose()
    except Exception as exc:
        log.warning("waterinfo_layers_failed", error=repr(exc))

    # --- 3. Natura 2000 (Mercator WFS boolean + sub-polygon distance) ---
    in_natura_2000 = False
    natura_2000_distance_m: float | None = None
    try:
        client_mc = create_http_client(settings, source="mercator")
        n2k_url = "/raadpleegdienstenmercatorpubliek/wfs"
        hbtrl = await _wfs(client_mc, n2k_url, "natura_2000_hbtrl",
                           "ps:ps_hbtrl", lat, lon, delta=0.001)
        vglrl = await _wfs(client_mc, n2k_url, "natura_2000_vglrl",
                           "ps:ps_vglrl", lat, lon, delta=0.001)
        in_natura_2000 = _has_features(hbtrl) or _has_features(vglrl)

        # Distance via the ps_hbtrl_deel sub-polygons: the whole-area
        # ps_hbtrl MultiPolygons span tens of km, so a first-vertex
        # approximation is meaningless there; the _deel sub-polygons are
        # small enough for _nearest_centroid_m to stay honest.
        deel = await _wfs(client_mc, n2k_url, "natura_2000_deel",
                          "ps:ps_hbtrl_deel", lat, lon, delta=0.05)
        natura_2000_distance_m = _nearest_centroid_m(deel, lat, lon)
        await client_mc.aclose()
    except Exception as exc:
        log.warning("natura_2000_failed", error=repr(exc))

    # --- 4. Heritage (WFS GetFeature) ---
    in_protected_heritage = False
    heritage_match_distance_m: float | None = None
    try:
        client_oe = create_http_client(settings, source="onroerend_erfgoed")
        vast_url = "/geoserver/wfs"
        # Boolean layer: vast_be features are building-sized, so the BBOX
        # must stay parcel-scale (~±55 m, matching the rule's <50 m proximity
        # criterion). The wide 0.05 default is a *distance-discovery* box —
        # using it here flips in_protected_heritage true for every address
        # in Flanders (any heritage within ~5.5 km counts as a hit).
        vast_data = await _wfs(client_oe, vast_url, "heritage_vast_be",
                               "vioe_geoportaal:vast_be", lat, lon,
                               delta=0.0005)
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
        log.warning("heritage_failed", error=repr(exc))

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
