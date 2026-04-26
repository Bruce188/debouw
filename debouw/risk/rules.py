"""
14-category rule functions for the debouw risk engine.

Each rule is a pure function: (FeatureSet, GeoOverlays | None, PermitProject) → RiskHit.
All rules are collected in _RULE_REGISTRY keyed by RiskCategory.
apply_all() iterates in sorted(RiskCategory) order for determinism.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from debouw.models.permit import GeoOverlays, PermitProject, RiskCategory
from debouw.risk.features import FeatureSet


@dataclass(frozen=True)
class RiskHit:
    """
    Result of one rule evaluation.

    Always returned — one per category. fired=False means the rule
    did not trigger; the scorer will produce a low probability but the
    factor is still included in the 14-vector (engine selects top-k later).
    Evidence strings are stable-formatted and sorted alphabetically.
    """

    category: RiskCategory
    fired: bool
    evidence: list[str]
    trigger_features: dict[str, float]


# ---------------------------------------------------------------------------
# 14 rule functions
# ---------------------------------------------------------------------------

def rule_gro_height(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when floors >= 5 OR height_m > 21, AND distance to residential < 30 m."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    tall = (features.floors is not None and features.floors >= 5) or (
        features.height_m is not None and features.height_m > 21
    )
    close = (
        features.distance_to_residential_m is not None
        and features.distance_to_residential_m < 30
    )

    if features.floors is not None:
        evidence.append(f"floors={features.floors:.0f}")
        trigger_feats["floors"] = features.floors
    if features.height_m is not None:
        evidence.append(f"height_m={features.height_m:.1f}")
        trigger_feats["height_m"] = features.height_m
    if features.distance_to_residential_m is not None:
        evidence.append(f"distance_to_residential_m={features.distance_to_residential_m:.1f}")
        trigger_feats["distance_to_residential_m"] = features.distance_to_residential_m

    fired = tall and close
    return RiskHit(
        category=RiskCategory.GRO_HEIGHT,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_water_flood(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when flood_risk_fluvial >= medium OR in_signaalgebied."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    fluvial_high = features.flood_risk_fluvial_ord >= 2
    pluvial_high = features.flood_risk_pluvial_ord >= 2
    signaal = features.in_signaalgebied

    if features.flood_risk_fluvial_ord > 0:
        evidence.append(f"flood_risk_fluvial_ord={features.flood_risk_fluvial_ord}")
        trigger_feats["flood_risk_fluvial_ord"] = float(features.flood_risk_fluvial_ord)
    if features.flood_risk_pluvial_ord > 0:
        evidence.append(f"flood_risk_pluvial_ord={features.flood_risk_pluvial_ord}")
        trigger_feats["flood_risk_pluvial_ord"] = float(features.flood_risk_pluvial_ord)
    if signaal:
        evidence.append("in_signaalgebied=True")
        trigger_feats["in_signaalgebied"] = 1.0

    fired = fluvial_high or pluvial_high or signaal
    return RiskHit(
        category=RiskCategory.WATER_FLOOD,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_mer_screening(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when mer_status in {none, screening} with units > 25 OR iioa_class in {1,2}."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    bad_mer = features.mer_status in (None, "none", "screening")
    large_project = features.units is not None and features.units > 25
    iioa_high = features.iioa_class in (1, 2)

    if features.mer_status is not None:
        evidence.append(f"mer_status={features.mer_status}")
    else:
        evidence.append("mer_status=none")
    if features.units is not None:
        evidence.append(f"units={features.units:.0f}")
        trigger_feats["units"] = features.units
    if features.iioa_class is not None:
        evidence.append(f"iioa_class={features.iioa_class}")
        trigger_feats["iioa_class"] = float(features.iioa_class)

    fired = bad_mer and (large_project or iioa_high)
    return RiskHit(
        category=RiskCategory.MER_SCREENING,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_bpa_rup_conflict(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when rup_zone is present AND (floors >= 5 OR height_m > 12)."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    rup = features.rup_zone_present
    tall = (features.floors is not None and features.floors >= 5) or (
        features.height_m is not None and features.height_m > 12
    )

    if rup:
        evidence.append("rup_zone=present")
        trigger_feats["rup_zone_present"] = 1.0
    if features.floors is not None:
        evidence.append(f"floors={features.floors:.0f}")
        trigger_feats["floors"] = features.floors
    if features.height_m is not None:
        evidence.append(f"height_m={features.height_m:.1f}")

    fired = rup and tall
    return RiskHit(
        category=RiskCategory.BPA_RUP_CONFLICT,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_motivation_defect(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """
    Universal RvVb ground — fires when any other feature is present (floors > 0 or units > 0).

    In Phase 2 this is a low-specificity universal trigger. Confidence is
    boosted indirectly when other categories also fire (engine-level concern).
    For the NEUTRAL_PROJECT (all None), this rule should NOT fire.
    """
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    has_floors = features.floors is not None and features.floors > 0
    has_units = features.units is not None and features.units > 0

    if features.floors is not None:
        evidence.append(f"floors={features.floors:.0f}")
        trigger_feats["floors"] = features.floors
    if features.units is not None:
        evidence.append(f"units={features.units:.0f}")
        trigger_feats["units"] = features.units

    fired = has_floors or has_units
    return RiskHit(
        category=RiskCategory.MOTIVATION_DEFECT,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_trees_kapverg(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when trees_to_fell >= 5 OR description mentions 'monumentaal/waardevol'."""
    import re
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    many_trees = features.trees_to_fell is not None and features.trees_to_fell >= 5
    desc_match = bool(re.search(r"\b(monumenta(a|le)|waardevo(l|lle))\b", features.description, re.I))

    if features.trees_to_fell is not None:
        evidence.append(f"trees_to_fell={features.trees_to_fell:.0f}")
        trigger_feats["trees_to_fell"] = features.trees_to_fell
    if desc_match:
        evidence.append("description_mentions=monumentaal_of_waardevol")

    fired = many_trees or desc_match
    return RiskHit(
        category=RiskCategory.TREES_KAPVERG,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_mobility_parking(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when parking_ratio < 0.8 (residential) or < 0.5 (urban/school proximity)."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    ratio = features.parking_ratio
    near_school = (
        features.distance_to_school_m is not None
        and features.distance_to_school_m < 300
    )
    threshold = 0.5 if near_school else 0.8

    if ratio is not None:
        evidence.append(f"parking_ratio={ratio:.2f}")
        trigger_feats["parking_ratio"] = ratio
    if features.distance_to_school_m is not None:
        evidence.append(f"distance_to_school_m={features.distance_to_school_m:.1f}")
        trigger_feats["distance_to_school_m"] = features.distance_to_school_m

    fired = ratio is not None and ratio < threshold
    return RiskHit(
        category=RiskCategory.MOBILITY_PARKING,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_nature_2000_n(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when in_natura_2000 OR (distance < 500 m AND iioa_class in {1,2})."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    inside = features.in_natura_2000
    near_and_noisy = (
        features.natura_2000_distance_m is not None
        and features.natura_2000_distance_m < 500
        and features.iioa_class in (1, 2)
    )

    if inside:
        evidence.append("in_natura_2000=True")
        trigger_feats["in_natura_2000"] = 1.0
    if features.natura_2000_distance_m is not None:
        evidence.append(f"natura_2000_distance_m={features.natura_2000_distance_m:.1f}")
        trigger_feats["natura_2000_distance_m"] = features.natura_2000_distance_m
    if features.iioa_class is not None:
        evidence.append(f"iioa_class={features.iioa_class}")
        trigger_feats["iioa_class"] = float(features.iioa_class)

    fired = inside or near_and_noisy
    return RiskHit(
        category=RiskCategory.NATURE_2000_N,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_heritage_inv(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when in_protected_heritage OR heritage_match_distance_m < 50 m."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    in_heritage = features.in_protected_heritage
    near_heritage = (
        features.heritage_match_distance_m is not None
        and features.heritage_match_distance_m < 50
    )

    if in_heritage:
        evidence.append("in_protected_heritage=True")
        trigger_feats["in_protected_heritage"] = 1.0
    if features.heritage_match_distance_m is not None:
        evidence.append(f"heritage_match_distance_m={features.heritage_match_distance_m:.1f}")
        trigger_feats["heritage_match_distance_m"] = features.heritage_match_distance_m

    fired = in_heritage or near_heritage
    return RiskHit(
        category=RiskCategory.HERITAGE_INV,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_nuisance_noise(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when iioa_class in {1,2} AND distance_to_residential_m < 50 m."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    iioa_high = features.iioa_class in (1, 2)
    close = (
        features.distance_to_residential_m is not None
        and features.distance_to_residential_m < 50
    )

    if features.iioa_class is not None:
        evidence.append(f"iioa_class={features.iioa_class}")
        trigger_feats["iioa_class"] = float(features.iioa_class)
    if features.distance_to_residential_m is not None:
        evidence.append(f"distance_to_residential_m={features.distance_to_residential_m:.1f}")
        trigger_feats["distance_to_residential_m"] = features.distance_to_residential_m

    fired = iioa_high and close
    return RiskHit(
        category=RiskCategory.NUISANCE_NOISE,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_privacy_bezonning(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when floors >= 4 AND distance_to_residential_m < 15 m."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    tall = features.floors is not None and features.floors >= 4
    close = (
        features.distance_to_residential_m is not None
        and features.distance_to_residential_m < 15
    )

    if features.floors is not None:
        evidence.append(f"floors={features.floors:.0f}")
        trigger_feats["floors"] = features.floors
    if features.distance_to_residential_m is not None:
        evidence.append(f"distance_to_residential_m={features.distance_to_residential_m:.1f}")
        trigger_feats["distance_to_residential_m"] = features.distance_to_residential_m

    fired = tall and close
    return RiskHit(
        category=RiskCategory.PRIVACY_BEZONNING,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_binding_advice_ignored(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when description mentions 'ongunstig advies', 'ANB', 'VMM', or 'OE'."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    mentions = features.mentions_ongunstig_advies

    if mentions:
        evidence.append("description_mentions=ongunstig_advies")
        trigger_feats["mentions_ongunstig_advies"] = 1.0

    return RiskHit(
        category=RiskCategory.BINDING_ADVICE_IGNORED,
        fired=mentions,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_function_mix_zoning(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """
    Fire when rup_zone_present AND project_type suggests a function mismatch.

    Heuristic: if rup_zone is present and the project has meaningful floors/units
    (suggesting residential or mixed use in a potentially incompatible zone),
    flag it. Phase 3+ will use a richer function-type lookup.
    """
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    rup = features.rup_zone_present
    has_project = (features.floors is not None and features.floors > 0) or (
        features.units is not None and features.units > 0
    )

    if rup:
        evidence.append("rup_zone=present")
        trigger_feats["rup_zone_present"] = 1.0
    if features.floors is not None:
        evidence.append(f"floors={features.floors:.0f}")
        trigger_feats["floors"] = features.floors

    fired = rup and has_project
    return RiskHit(
        category=RiskCategory.FUNCTION_MIX_ZONING,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


def rule_vergunningencarrousel(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> RiskHit:
    """Fire when parcel_repeat_count >= 2 (same parcel seen ≥2× in last 24 months)."""
    evidence: list[str] = []
    trigger_feats: dict[str, float] = {}

    repeat = features.parcel_repeat_count
    if repeat > 0:
        evidence.append(f"parcel_repeat_count={repeat}")
        trigger_feats["parcel_repeat_count"] = float(repeat)

    fired = repeat >= 2
    return RiskHit(
        category=RiskCategory.VERGUNNINGENCARROUSEL,
        fired=fired,
        evidence=sorted(evidence),
        trigger_features=trigger_feats,
    )


# ---------------------------------------------------------------------------
# Registry and apply_all
# ---------------------------------------------------------------------------

_RULE_REGISTRY: Mapping[RiskCategory, Callable[..., RiskHit]] = {
    RiskCategory.GRO_HEIGHT: rule_gro_height,
    RiskCategory.WATER_FLOOD: rule_water_flood,
    RiskCategory.MER_SCREENING: rule_mer_screening,
    RiskCategory.BPA_RUP_CONFLICT: rule_bpa_rup_conflict,
    RiskCategory.MOTIVATION_DEFECT: rule_motivation_defect,
    RiskCategory.TREES_KAPVERG: rule_trees_kapverg,
    RiskCategory.MOBILITY_PARKING: rule_mobility_parking,
    RiskCategory.NATURE_2000_N: rule_nature_2000_n,
    RiskCategory.HERITAGE_INV: rule_heritage_inv,
    RiskCategory.NUISANCE_NOISE: rule_nuisance_noise,
    RiskCategory.PRIVACY_BEZONNING: rule_privacy_bezonning,
    RiskCategory.BINDING_ADVICE_IGNORED: rule_binding_advice_ignored,
    RiskCategory.FUNCTION_MIX_ZONING: rule_function_mix_zoning,
    RiskCategory.VERGUNNINGENCARROUSEL: rule_vergunningencarrousel,
}


def apply_all(
    features: FeatureSet,
    overlays: GeoOverlays | None,
    project: PermitProject,
) -> list[RiskHit]:
    """
    Apply all 14 rules in sorted(RiskCategory) enum-value order.

    Returns exactly 14 RiskHit instances — one per category. Ordering is
    deterministic: categories are sorted by their string value alphabetically.
    """
    return [
        _RULE_REGISTRY[cat](features, overlays, project)
        for cat in sorted(RiskCategory, key=lambda c: c.value)
    ]
