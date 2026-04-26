"""
Synthetic PermitProject fixtures for risk engine testing.

70 fixtures: 5 archetypes × 14 RiskCategory values. Each archetype is
tuned to fire the target category and (where possible) not others.
A NEUTRAL_PROJECT fires no rules.

All fixtures use:
- decision_regime="post_2026_reform"
- status=PermitProjectStatus.INTAKE
- first_seen_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
- applicant_name=None (GDPR)
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from debouw.models.permit import (
    Address,
    GeoOverlays,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
    RiskCategory,
)

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)
_POINT = GeoPoint(lat=51.0543, lon=3.7174)
_BASE_URL = "https://gent.consultatieomgeving.net/burger/dossier/{eid}"


def _addr(parcel_id: str | None = None) -> Address:
    return Address(
        raw="Teststraat 1, 9000 Gent",
        street="Teststraat",
        house_number="1",
        postcode="9000",
        municipality="Gent",
        point=_POINT,
        parcel_id=parcel_id,
    )


def _proj(
    eid: str,
    *,
    floors: int | None = None,
    height_m: float | None = None,
    units: int | None = None,
    parking_spaces: int | None = None,
    trees_to_fell: int | None = None,
    mer_status=None,
    iioa_class: int | None = None,
    overlays: GeoOverlays | None = None,
    description: str | None = None,
    project_type: str | None = None,
    parcel_id: str | None = None,
) -> PermitProject:
    return PermitProject(
        external_id=f"synthetic:{eid}",
        source="gent_consultatie",
        omv_reference=eid,
        detail_url=_BASE_URL.format(eid=eid),
        title=f"Synthetic fixture {eid}",
        description=description,
        applicant_name=None,
        address=_addr(parcel_id=parcel_id),
        project_type=project_type,
        floors=floors,
        height_m=height_m,
        units=units,
        parking_spaces=parking_spaces,
        trees_to_fell=trees_to_fell,
        mer_status=mer_status,
        iioa_class=iioa_class,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=overlays,
        raw_html_path=Path("/tmp/synthetic.html"),
        first_seen_at=_T,
        last_changed_at=_T,
        content_hash="a" * 64,
        decision_regime="post_2026_reform",
    )


# ---------------------------------------------------------------------------
# Helper overlays
# ---------------------------------------------------------------------------

def _overlays(
    in_natura_2000: bool = False,
    natura_2000_distance_m: float | None = None,
    in_signaalgebied: bool = False,
    flood_risk_fluvial: str = "none",
    flood_risk_pluvial: str = "none",
    in_protected_heritage: bool = False,
    heritage_match_distance_m: float | None = None,
    rup_zone: str | None = None,
    bpa_voorschriften_text: str | None = None,
    distance_to_school_m: float | None = None,
    distance_to_residential_m: float | None = None,
) -> GeoOverlays:
    return GeoOverlays(
        in_natura_2000=in_natura_2000,
        natura_2000_distance_m=natura_2000_distance_m,
        in_signaalgebied=in_signaalgebied,
        flood_risk_fluvial=flood_risk_fluvial,
        flood_risk_pluvial=flood_risk_pluvial,
        in_protected_heritage=in_protected_heritage,
        heritage_match_distance_m=heritage_match_distance_m,
        rup_zone=rup_zone,
        bpa_voorschriften_text=bpa_voorschriften_text,
        distance_to_school_m=distance_to_school_m,
        distance_to_residential_m=distance_to_residential_m,
    )


# ---------------------------------------------------------------------------
# GRO_HEIGHT fixtures (5 archetypes)
# ---------------------------------------------------------------------------

GRO_HEIGHT_FIXTURES = [
    _proj("gro_height:0", floors=8, height_m=28.0,
          overlays=_overlays(distance_to_residential_m=15.0)),
    _proj("gro_height:1", floors=6, height_m=22.0,
          overlays=_overlays(distance_to_residential_m=20.0)),
    _proj("gro_height:2", floors=7, height_m=25.0, units=40,
          overlays=_overlays(distance_to_residential_m=10.0)),
    _proj("gro_height:3", floors=5, height_m=21.5,
          overlays=_overlays(distance_to_residential_m=25.0)),
    _proj("gro_height:4", floors=9, height_m=32.0, units=60,
          overlays=_overlays(distance_to_residential_m=12.0)),
]

# ---------------------------------------------------------------------------
# WATER_FLOOD fixtures
# ---------------------------------------------------------------------------

WATER_FLOOD_FIXTURES = [
    _proj("water_flood:0", units=30,
          overlays=_overlays(flood_risk_fluvial="high", in_signaalgebied=True)),
    _proj("water_flood:1", units=20,
          overlays=_overlays(flood_risk_fluvial="medium")),
    _proj("water_flood:2", units=15,
          overlays=_overlays(flood_risk_pluvial="medium", in_signaalgebied=True)),
    _proj("water_flood:3", units=50,
          overlays=_overlays(flood_risk_fluvial="high", flood_risk_pluvial="high")),
    _proj("water_flood:4", units=10,
          overlays=_overlays(in_signaalgebied=True)),
]

# ---------------------------------------------------------------------------
# MER_SCREENING fixtures
# ---------------------------------------------------------------------------

MER_SCREENING_FIXTURES = [
    _proj("mer_screening:0", units=40, iioa_class=1, mer_status="none"),
    _proj("mer_screening:1", units=30, iioa_class=2, mer_status="screening"),
    _proj("mer_screening:2", units=60, mer_status="none"),
    _proj("mer_screening:3", units=26, iioa_class=1),
    _proj("mer_screening:4", units=50, iioa_class=2, mer_status="screening"),
]

# ---------------------------------------------------------------------------
# BPA_RUP_CONFLICT fixtures
# ---------------------------------------------------------------------------

BPA_RUP_CONFLICT_FIXTURES = [
    _proj("bpa_rup:0", floors=6, height_m=22.0,
          overlays=_overlays(rup_zone="woongebied", bpa_voorschriften_text="max 3 bouwlagen")),
    _proj("bpa_rup:1", floors=5,
          overlays=_overlays(rup_zone="industriegebied")),
    _proj("bpa_rup:2", floors=7, height_m=25.0,
          overlays=_overlays(rup_zone="gemengd woongebied")),
    _proj("bpa_rup:3", floors=5, units=20,
          overlays=_overlays(rup_zone="parkgebied")),
    _proj("bpa_rup:4", floors=8, height_m=30.0,
          overlays=_overlays(rup_zone="woonuitbreidingsgebied")),
]

# ---------------------------------------------------------------------------
# MOTIVATION_DEFECT fixtures
# ---------------------------------------------------------------------------

MOTIVATION_DEFECT_FIXTURES = [
    _proj("motivation:0", floors=4, units=15),
    _proj("motivation:1", floors=2, units=30),
    _proj("motivation:2", floors=6, units=50),
    _proj("motivation:3", floors=1, units=10),
    _proj("motivation:4", floors=3, units=20),
]

# ---------------------------------------------------------------------------
# TREES_KAPVERG fixtures
# ---------------------------------------------------------------------------

TREES_KAPVERG_FIXTURES = [
    _proj("trees:0", trees_to_fell=10),
    _proj("trees:1", trees_to_fell=7, description="Kap van 7 monumentale bomen"),
    _proj("trees:2", trees_to_fell=5),
    _proj("trees:3", trees_to_fell=12, description="Waardevolle linde-rij"),
    _proj("trees:4", trees_to_fell=8),
]

# ---------------------------------------------------------------------------
# MOBILITY_PARKING fixtures
# ---------------------------------------------------------------------------

MOBILITY_PARKING_FIXTURES = [
    _proj("parking:0", units=50, parking_spaces=5,
          overlays=_overlays(distance_to_school_m=100.0)),  # ratio=0.1, very low
    _proj("parking:1", units=40, parking_spaces=4),         # ratio=0.1 < 0.8 threshold
    _proj("parking:2", units=60, parking_spaces=6,
          overlays=_overlays(distance_to_school_m=150.0)),  # ratio=0.1 < 0.5
    _proj("parking:3", units=30, parking_spaces=3,
          overlays=_overlays(distance_to_school_m=80.0)),   # ratio=0.1 < 0.5
    _proj("parking:4", units=80, parking_spaces=8),         # ratio=0.1 < 0.8
]

# ---------------------------------------------------------------------------
# NATURE_2000_N fixtures
# ---------------------------------------------------------------------------

NATURE_2000_N_FIXTURES = [
    _proj("nature:0", iioa_class=1,
          overlays=_overlays(in_natura_2000=True)),
    _proj("nature:1", iioa_class=2,
          overlays=_overlays(natura_2000_distance_m=200.0)),
    _proj("nature:2", iioa_class=1,
          overlays=_overlays(natura_2000_distance_m=100.0)),
    _proj("nature:3", iioa_class=2,
          overlays=_overlays(in_natura_2000=True, natura_2000_distance_m=50.0)),
    _proj("nature:4", iioa_class=1,
          overlays=_overlays(natura_2000_distance_m=300.0)),
]

# ---------------------------------------------------------------------------
# HERITAGE_INV fixtures
# ---------------------------------------------------------------------------

HERITAGE_INV_FIXTURES = [
    _proj("heritage:0", floors=3,
          overlays=_overlays(in_protected_heritage=True)),
    _proj("heritage:1", floors=2,
          overlays=_overlays(heritage_match_distance_m=30.0)),
    _proj("heritage:2", floors=4,
          overlays=_overlays(in_protected_heritage=True, heritage_match_distance_m=20.0)),
    _proj("heritage:3", floors=1,
          overlays=_overlays(heritage_match_distance_m=10.0)),
    _proj("heritage:4", floors=3,
          overlays=_overlays(in_protected_heritage=True, heritage_match_distance_m=45.0)),
]

# ---------------------------------------------------------------------------
# NUISANCE_NOISE fixtures
# ---------------------------------------------------------------------------

NUISANCE_NOISE_FIXTURES = [
    _proj("noise:0", iioa_class=1,
          overlays=_overlays(distance_to_residential_m=3.0)),   # very close
    _proj("noise:1", iioa_class=2,
          overlays=_overlays(distance_to_residential_m=5.0)),
    _proj("noise:2", iioa_class=1,
          overlays=_overlays(distance_to_residential_m=2.0)),
    _proj("noise:3", iioa_class=2,
          overlays=_overlays(distance_to_residential_m=8.0)),
    _proj("noise:4", iioa_class=1,
          overlays=_overlays(distance_to_residential_m=1.0)),
]

# ---------------------------------------------------------------------------
# PRIVACY_BEZONNING fixtures
# ---------------------------------------------------------------------------

PRIVACY_BEZONNING_FIXTURES = [
    _proj("privacy:0", floors=8,
          overlays=_overlays(distance_to_residential_m=3.0)),   # very tall, very close
    _proj("privacy:1", floors=7,
          overlays=_overlays(distance_to_residential_m=4.0)),
    _proj("privacy:2", floors=10,
          overlays=_overlays(distance_to_residential_m=2.0)),
    _proj("privacy:3", floors=6,
          overlays=_overlays(distance_to_residential_m=5.0)),
    _proj("privacy:4", floors=9,
          overlays=_overlays(distance_to_residential_m=1.0)),
]

# ---------------------------------------------------------------------------
# BINDING_ADVICE_IGNORED fixtures
# ---------------------------------------------------------------------------

BINDING_ADVICE_IGNORED_FIXTURES = [
    _proj("binding:0",
          description="Dossier bevat een ongunstig advies van ANB dat niet werd gevolgd."),
    _proj("binding:1",
          description="Advies van VMM is ongunstig; vergunning werd toch verleend."),
    _proj("binding:2",
          description="OE gaf een ongunstig advies over de impact op het erfgoed."),
    _proj("binding:3",
          description="Het ongunstig advies van ANB werd niet meegenomen in de motivering."),
    _proj("binding:4",
          description="VMM verleende een ongunstig advies wegens overstromingsrisico."),
]

# ---------------------------------------------------------------------------
# FUNCTION_MIX_ZONING fixtures
# ---------------------------------------------------------------------------

FUNCTION_MIX_ZONING_FIXTURES = [
    _proj("zoning:0", floors=4, units=20, project_type="wonen",
          overlays=_overlays(rup_zone="industriegebied")),
    _proj("zoning:1", floors=2, units=10, project_type="kantoren",
          overlays=_overlays(rup_zone="woongebied")),
    _proj("zoning:2", floors=5, units=30, project_type="gemengd",
          overlays=_overlays(rup_zone="parkgebied")),
    _proj("zoning:3", floors=1, units=5, project_type="horeca",
          overlays=_overlays(rup_zone="agrarisch gebied")),
    _proj("zoning:4", floors=3, units=15, project_type="handel",
          overlays=_overlays(rup_zone="woonuitbreidingsgebied")),
]

# ---------------------------------------------------------------------------
# VERGUNNINGENCARROUSEL fixtures
# ---------------------------------------------------------------------------

VERGUNNINGENCARROUSEL_FIXTURES = [
    _proj("carrousel:0", floors=2, units=5, parcel_id="12345A"),
    _proj("carrousel:1", floors=3, units=8, parcel_id="12345B"),
    _proj("carrousel:2", floors=1, units=3, parcel_id="12345C"),
    _proj("carrousel:3", floors=2, units=6, parcel_id="12345D"),
    _proj("carrousel:4", floors=4, units=10, parcel_id="12345E"),
]

# For the carrousel fixtures we need parcel_repeat_count >= 2 to fire.
# In tests, parcel_repeat_count is passed explicitly to extract().
# The fixture projects themselves just need valid structure.

# ---------------------------------------------------------------------------
# SYNTHETIC_PROJECTS mapping
# ---------------------------------------------------------------------------

SYNTHETIC_PROJECTS: Mapping[RiskCategory, list[PermitProject]] = {
    RiskCategory.GRO_HEIGHT: GRO_HEIGHT_FIXTURES,
    RiskCategory.WATER_FLOOD: WATER_FLOOD_FIXTURES,
    RiskCategory.MER_SCREENING: MER_SCREENING_FIXTURES,
    RiskCategory.BPA_RUP_CONFLICT: BPA_RUP_CONFLICT_FIXTURES,
    RiskCategory.MOTIVATION_DEFECT: MOTIVATION_DEFECT_FIXTURES,
    RiskCategory.TREES_KAPVERG: TREES_KAPVERG_FIXTURES,
    RiskCategory.MOBILITY_PARKING: MOBILITY_PARKING_FIXTURES,
    RiskCategory.NATURE_2000_N: NATURE_2000_N_FIXTURES,
    RiskCategory.HERITAGE_INV: HERITAGE_INV_FIXTURES,
    RiskCategory.NUISANCE_NOISE: NUISANCE_NOISE_FIXTURES,
    RiskCategory.PRIVACY_BEZONNING: PRIVACY_BEZONNING_FIXTURES,
    RiskCategory.BINDING_ADVICE_IGNORED: BINDING_ADVICE_IGNORED_FIXTURES,
    RiskCategory.FUNCTION_MIX_ZONING: FUNCTION_MIX_ZONING_FIXTURES,
    RiskCategory.VERGUNNINGENCARROUSEL: VERGUNNINGENCARROUSEL_FIXTURES,
}

# ---------------------------------------------------------------------------
# NEUTRAL_PROJECT — fires no rules
# ---------------------------------------------------------------------------

NEUTRAL_PROJECT = _proj(
    "neutral:0",
    floors=None,
    height_m=None,
    units=None,
    parking_spaces=None,
    trees_to_fell=None,
    mer_status=None,
    iioa_class=None,
    overlays=_overlays(),  # all default (no flood, no natura, no heritage, no rup)
    description="",
)
