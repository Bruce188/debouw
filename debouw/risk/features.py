"""
Feature extraction for the debouw risk engine.

FeatureSet is a frozen Pydantic model covering all beta-key fields referenced
by any RiskCategoryDef in taxonomy.py. The extract() function is pure — no
network calls, no DB queries.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from debouw.models.permit import GeoOverlays, PermitProject

# Ordinal mapping for flood risk literals
_FLOOD_ORD: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Regex for binding-advice mentions (Phase 2: regex; Sonnet upgrade in Phase 3)
_ONGUNSTIG_RE = re.compile(r"\b(ongunstig advies|ANB|VMM|OE)\b", re.IGNORECASE)


class FeatureSet(BaseModel):
    """
    Frozen feature vector derived from PermitProject + GeoOverlays.

    All fields correspond to beta-weight keys used in taxonomy.py entries.
    Missing / inapplicable values default to None or False/0 so scoring
    functions can safely treat them as zero-contribution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Project geometry ---
    floors: float | None = None
    height_m: float | None = None
    units: float | None = None
    parking_spaces: float | None = None
    parking_ratio: float | None = None       # parking_spaces / units
    trees_to_fell: float | None = None

    # --- Environmental / overlay ---
    iioa_class: int | None = None
    mer_status: str | None = None            # raw literal from PermitProject
    mer_status_none_ord: float = 0.0         # 1.0 if mer_status in {None, "none"}
    in_natura_2000: bool = False
    natura_2000_distance_m: float | None = None
    natura_2000_distance_m_inv: float = 0.0  # 1/distance (capped), 0 when None
    flood_risk_fluvial_ord: int = 0          # 0-3
    flood_risk_pluvial_ord: int = 0          # 0-3
    in_signaalgebied: bool = False
    in_protected_heritage: bool = False
    heritage_match_distance_m: float | None = None
    heritage_match_distance_m_inv: float = 0.0

    # --- Spatial context ---
    distance_to_residential_m: float | None = None
    distance_to_residential_m_inv: float = 0.0  # 1/distance (capped), 0 when None
    distance_to_school_m: float | None = None
    distance_to_school_m_inv: float = 0.0

    # --- Planning context ---
    rup_zone_present: bool = False
    bpa_voorschriften_text: str | None = None

    # --- Vergunningencarrousel ---
    parcel_repeat_count: int = 0

    # --- Text-derived ---
    description: str = ""
    mentions_ongunstig_advies: bool = False


def _inv_capped(d: float | None, cap: float = 500.0) -> float:
    """Return 1/d capped at 1/1 (d=1 → 1.0), 0 when d is None or 0."""
    if d is None or d <= 0:
        return 0.0
    return min(1.0, 1.0 / d) if d >= 1.0 else 1.0


def extract(
    project: PermitProject,
    overlays: GeoOverlays | None = None,
    *,
    parcel_repeat_count: int = 0,
) -> FeatureSet:
    """
    Extract a FeatureSet from a project (and optional overlays).

    Pure function: no network calls, no DB queries. Deterministic given
    the same inputs.
    """
    o = overlays or project.overlays  # prefer explicit overlays arg

    # Geo overlay fields
    in_natura_2000 = o.in_natura_2000 if o else False
    natura_2000_distance_m = o.natura_2000_distance_m if o else None
    in_signaalgebied = o.in_signaalgebied if o else False
    flood_risk_fluvial_ord = _FLOOD_ORD.get(o.flood_risk_fluvial, 0) if o else 0
    flood_risk_pluvial_ord = _FLOOD_ORD.get(o.flood_risk_pluvial, 0) if o else 0
    in_protected_heritage = o.in_protected_heritage if o else False
    heritage_match_distance_m = o.heritage_match_distance_m if o else None
    rup_zone_present = bool(o.rup_zone) if o else False
    bpa_voorschriften_text = o.bpa_voorschriften_text if o else None
    distance_to_residential_m = o.distance_to_residential_m if o else None
    distance_to_school_m = o.distance_to_school_m if o else None

    # Parking ratio
    parking_ratio: float | None = None
    if project.parking_spaces is not None and project.units and project.units > 0:
        parking_ratio = project.parking_spaces / project.units

    # MER ordinal: 1.0 when status is effectively absent
    mer_status_none_ord = 1.0 if project.mer_status in (None, "none") else 0.0

    # Text-derived: binding advice ignored
    desc = project.description or ""
    mentions_ongunstig = bool(_ONGUNSTIG_RE.search(desc))

    return FeatureSet(
        floors=float(project.floors) if project.floors is not None else None,
        height_m=project.height_m,
        units=float(project.units) if project.units is not None else None,
        parking_spaces=float(project.parking_spaces) if project.parking_spaces is not None else None,
        parking_ratio=parking_ratio,
        trees_to_fell=float(project.trees_to_fell) if project.trees_to_fell is not None else None,
        iioa_class=project.iioa_class,
        mer_status=project.mer_status,
        mer_status_none_ord=mer_status_none_ord,
        in_natura_2000=in_natura_2000,
        natura_2000_distance_m=natura_2000_distance_m,
        natura_2000_distance_m_inv=_inv_capped(natura_2000_distance_m),
        flood_risk_fluvial_ord=flood_risk_fluvial_ord,
        flood_risk_pluvial_ord=flood_risk_pluvial_ord,
        in_signaalgebied=in_signaalgebied,
        in_protected_heritage=in_protected_heritage,
        heritage_match_distance_m=heritage_match_distance_m,
        heritage_match_distance_m_inv=_inv_capped(heritage_match_distance_m),
        distance_to_residential_m=distance_to_residential_m,
        distance_to_residential_m_inv=_inv_capped(distance_to_residential_m),
        distance_to_school_m=distance_to_school_m,
        distance_to_school_m_inv=_inv_capped(distance_to_school_m),
        rup_zone_present=rup_zone_present,
        bpa_voorschriften_text=bpa_voorschriften_text,
        parcel_repeat_count=parcel_repeat_count,
        description=desc,
        mentions_ongunstig_advies=mentions_ongunstig,
    )
