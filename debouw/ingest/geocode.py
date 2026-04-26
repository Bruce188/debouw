"""
Nominatim geocoder with 30-day file cache.

Geocodes Belgian addresses using OpenStreetMap Nominatim.
Empty addresses short-circuit immediately (no network call).
Results are cached by sha256(normalised_address) with a 30-day mtime TTL.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import structlog

from debouw.config import Settings
from debouw.ingest.http import create_http_client
from debouw.models.permit import GeoPoint

log = structlog.get_logger(__name__)

# Belgium bounding box (inclusive)
_LAT_MIN, _LAT_MAX = 49.5, 51.6
_LON_MIN, _LON_MAX = 2.5, 6.4
_CACHE_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# Cache helpers (≤40 LoC)
# ---------------------------------------------------------------------------

def _cache_path(address: str, settings: Settings) -> Path:
    key = hashlib.sha256(address.strip().lower().encode()).hexdigest()
    return settings.data_root / "cache" / "geocode" / f"{key}.json"


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
# Public API
# ---------------------------------------------------------------------------

async def geocode(address: str, settings: Settings) -> GeoPoint | None:
    """Geocode a Belgian address string via Nominatim.

    Returns None on empty input, cache miss, or coordinates outside Belgium.
    """
    if not address.strip():
        return None

    # UA warning (do not raise — default UA is a placeholder for CI)
    if settings.nominatim_user_agent.startswith("debouw-research/0.x (set "):
        log.warning("nominatim_default_ua", ua=settings.nominatim_user_agent)

    path = _cache_path(address, settings)
    cached = _load_cache(path)
    if cached is not None:
        if cached.get("hit") is False:
            return None  # cached miss
        return GeoPoint(lat=cached["lat"], lon=cached["lon"])

    client = create_http_client(settings, source="nominatim")
    try:
        response = await client.get(
            "/search",
            params={
                "q": address.strip(),
                "format": "json",
                "countrycodes": "be",
                "limit": 1,
                "addressdetails": 0,
            },
        )
        hits = response.json()
        if not hits:
            _save_cache(path, {"hit": False})
            return None

        hit = hits[0]
        lat = float(hit["lat"])
        lon = float(hit["lon"])

        # Validate against Belgium bbox
        if not (_LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX):
            _save_cache(path, {"hit": False})
            return None

        point = GeoPoint(lat=lat, lon=lon)
        _save_cache(path, {"lat": lat, "lon": lon})
        return point

    finally:
        await client.aclose()
