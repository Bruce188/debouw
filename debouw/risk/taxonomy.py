"""
14-category risk taxonomy for the debouw omgevingsvergunning risk engine.

Each RiskCategoryDef is the single source of truth for:
- rule scoring (beta_weights, base_success_rate, severity_prior_days)
- narrator system prompt (label_nl, legal_basis_nl, typical_objector_template_nl)
- static-template fallback (static_rationale_nl)
- project_modifier callable for context-sensitive severity
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from debouw.models.permit import GeoOverlays, PermitProject, RiskCategory


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Project modifier helpers
# ---------------------------------------------------------------------------

def project_modifier_default(p: PermitProject, o: GeoOverlays | None) -> float:
    return 1.0


def project_modifier_water_flood(p: PermitProject, o: GeoOverlays | None) -> float:
    """Higher-unit projects face longer water-related delays."""
    return _clip(1.0 + 0.2 * ((p.units or 0) / 50), 0.7, 1.4)


def project_modifier_height(p: PermitProject, o: GeoOverlays | None) -> float:
    """Taller buildings (>5 floors) face incrementally longer height-conflict delays."""
    return _clip(1.0 + 0.05 * max(0, (p.floors or 0) - 5), 0.7, 1.4)


def project_modifier_mer(p: PermitProject, o: GeoOverlays | None) -> float:
    """Larger unit count and IIOA class 1/2 increase MER delay risk."""
    base = 1.0
    if p.units and p.units > 50:
        base += 0.2
    if p.iioa_class in (1, 2):
        base += 0.2
    return _clip(base, 0.7, 1.4)


def project_modifier_nature(p: PermitProject, o: GeoOverlays | None) -> float:
    """Natura 2000 proximity increases nitrogen assessment delays."""
    if o and o.natura_2000_distance_m is not None and o.natura_2000_distance_m < 250:
        return 1.3
    if p.iioa_class in (1, 2):
        return 1.2
    return 1.0


def project_modifier_parking(p: PermitProject, o: GeoOverlays | None) -> float:
    """School proximity multiplies parking-shortage delays."""
    if o and o.distance_to_school_m is not None and o.distance_to_school_m < 200:
        return 1.2
    return 1.0


def project_modifier_trees(p: PermitProject, o: GeoOverlays | None) -> float:
    """More trees to fell → longer kapvergunning procedures."""
    trees = p.trees_to_fell or 0
    return _clip(1.0 + 0.04 * max(0, trees - 5), 0.7, 1.4)


def project_modifier_carrousel(p: PermitProject, o: GeoOverlays | None) -> float:
    """Repeated parcel applications compound procedural delays."""
    return 1.2


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskCategoryDef:
    id: RiskCategory
    label_nl: str
    label_en: str
    severity_prior_days: int            # 30..540
    base_success_rate: float            # 0.05..0.95
    beta_weights: Mapping[str, float]   # keys must be FeatureSet field names
    evidence_keys: tuple[str, ...]      # cited in RiskFactor.evidence
    typical_objector_template_nl: str
    legal_basis_nl: str
    static_rationale_nl: str            # confidence < 0.3 fallback
    project_modifier: Callable[[PermitProject, GeoOverlays | None], float]


# ---------------------------------------------------------------------------
# 14 category definitions
# ---------------------------------------------------------------------------

GRO_HEIGHT_DEF = RiskCategoryDef(
    id=RiskCategory.GRO_HEIGHT,
    label_nl="Schaal & bouwhoogte conflict",
    label_en="Scale & building height conflict",
    severity_prior_days=240,
    base_success_rate=0.55,
    beta_weights={
        "floors": 0.4,
        "height_m": 0.05,
        "distance_to_residential_m_inv": 0.3,
    },
    evidence_keys=("floors", "height_m", "distance_to_residential_m"),
    typical_objector_template_nl="omwonenden binnen 50 m",
    legal_basis_nl="art. 4.3.1 §2 VCRO",
    static_rationale_nl=(
        "Onvoldoende gegevens om de schaal- en hoogte-impact betrouwbaar te beoordelen; "
        "verifieer manueel de verhouding tussen gebouwhoogte en omringende bebouwing."
    ),
    project_modifier=project_modifier_height,
)

WATER_FLOOD_DEF = RiskCategoryDef(
    id=RiskCategory.WATER_FLOOD,
    label_nl="Watertoets / overstromingsgevoelig",
    label_en="Water test / flood risk",
    severity_prior_days=270,
    base_success_rate=0.60,
    beta_weights={
        "flood_risk_fluvial_ord": 0.5,
        "flood_risk_pluvial_ord": 0.35,
        "in_signaalgebied": 0.4,
    },
    evidence_keys=("flood_risk_fluvial", "flood_risk_pluvial", "in_signaalgebied"),
    typical_objector_template_nl="Departement Omgeving / VMM",
    legal_basis_nl="art. 9/1 Watertoetsbesluit; art. 4.3.2 DABM",
    static_rationale_nl=(
        "Onvoldoende gegevens om het overstromingsrisico betrouwbaar te beoordelen; "
        "raadpleeg de watertoets via het Geopunt-loket."
    ),
    project_modifier=project_modifier_water_flood,
)

MER_SCREENING_DEF = RiskCategoryDef(
    id=RiskCategory.MER_SCREENING,
    label_nl="Project-MER ontoereikend",
    label_en="Project EIA inadequate",
    severity_prior_days=200,
    base_success_rate=0.45,
    beta_weights={
        "mer_status_none_ord": 0.6,
        "iioa_class": 0.4,
        "units": 0.015,
    },
    evidence_keys=("mer_status", "iioa_class", "units"),
    typical_objector_template_nl="Departement Omgeving / omwonenden met MER-expertise",
    legal_basis_nl="art. 4.3.2 DABM; bijlage II MER-besluit",
    static_rationale_nl=(
        "De MER-screening kan ontoereikend zijn; verifieer de drempelwaarden in "
        "bijlage II van het MER-besluit en art. 4.3.2 DABM."
    ),
    project_modifier=project_modifier_mer,
)

BPA_RUP_CONFLICT_DEF = RiskCategoryDef(
    id=RiskCategory.BPA_RUP_CONFLICT,
    label_nl="Strijdig met BPA/RUP",
    label_en="Conflicts with local land-use plan",
    severity_prior_days=320,
    base_success_rate=0.65,
    beta_weights={
        "rup_zone_present": 0.5,
        "floors": 0.3,
        "height_m": 0.02,
    },
    evidence_keys=("rup_zone", "bpa_voorschriften_text", "floors"),
    typical_objector_template_nl="buren / bewonersgroepen die BPA/RUP bewaken",
    legal_basis_nl="art. 4.3.1 VCRO; gemeentelijk BPA of provinciaal RUP",
    static_rationale_nl=(
        "Onvoldoende gegevens om strijdigheid met het BPA of RUP vast te stellen; "
        "raadpleeg de stedenbouwkundige voorschriften van de betrokken zone."
    ),
    project_modifier=project_modifier_default,
)

MOTIVATION_DEFECT_DEF = RiskCategoryDef(
    id=RiskCategory.MOTIVATION_DEFECT,
    label_nl="Onafdoende motivering art. 4.3.1 §2 VCRO",
    label_en="Inadequate motivation (VCRO)",
    severity_prior_days=150,
    base_success_rate=0.70,
    beta_weights={
        "floors": 0.1,
        "units": 0.008,
    },
    evidence_keys=("floors", "units"),
    typical_objector_template_nl="elke procesbekwame derde",
    legal_basis_nl="art. 4.3.1 §2 VCRO; RvVb vaste rechtspraak",
    static_rationale_nl=(
        "Een motiveringsgebrek is een universele RvVb-grond; verifieer of de "
        "vergunningsbeslissing de goede ruimtelijke ordening voldoende motiveert."
    ),
    project_modifier=project_modifier_default,
)

TREES_KAPVERG_DEF = RiskCategoryDef(
    id=RiskCategory.TREES_KAPVERG,
    label_nl="Kapvergunning waardevolle bomen",
    label_en="Valuable tree removal permit",
    severity_prior_days=90,
    base_success_rate=0.40,
    beta_weights={
        "trees_to_fell": 0.25,
    },
    evidence_keys=("trees_to_fell",),
    typical_objector_template_nl="buurtcomités / Groene Kring / Natuurpunt",
    legal_basis_nl="art. 4.4.23 VCRO; gemeentelijke bomenverordening",
    static_rationale_nl=(
        "Onvoldoende gegevens over het aantal en de waarde van te kappen bomen; "
        "verifieer of een kapvergunning vereist is via de gemeentelijke bomenverordening."
    ),
    project_modifier=project_modifier_trees,
)

MOBILITY_PARKING_DEF = RiskCategoryDef(
    id=RiskCategory.MOBILITY_PARKING,
    label_nl="Parkeernorm tekort",
    label_en="Parking standard deficit",
    severity_prior_days=120,
    base_success_rate=0.35,
    beta_weights={
        "parking_ratio": -2.5,
        "distance_to_school_m_inv": 1.5,
    },
    evidence_keys=("parking_spaces", "units", "distance_to_school_m"),
    typical_objector_template_nl="buurtbewoners / mobiliteitsafdeling gemeente",
    legal_basis_nl="gemeentelijk parkeerreglement; art. 4.3.1 VCRO mobiliteitstoets",
    static_rationale_nl=(
        "Onvoldoende gegevens om de parkeerratio te berekenen; verifieer de "
        "parkeernormen in het gemeentelijk parkeerreglement."
    ),
    project_modifier=project_modifier_parking,
)

NATURE_2000_N_DEF = RiskCategoryDef(
    id=RiskCategory.NATURE_2000_N,
    label_nl="Passende beoordeling / stikstof",
    label_en="Appropriate assessment / nitrogen",
    severity_prior_days=360,
    base_success_rate=0.55,
    beta_weights={
        "in_natura_2000": 0.6,
        "natura_2000_distance_m_inv": 0.3,
        "iioa_class": 0.2,
    },
    evidence_keys=("in_natura_2000", "natura_2000_distance_m", "iioa_class"),
    typical_objector_template_nl="ANB / Natuurpunt / Vogelbescherming Vlaanderen",
    legal_basis_nl="art. 36ter Decreet Natuurbehoud; Habitatrichtlijn art. 6",
    static_rationale_nl=(
        "Onvoldoende gegevens over de ligging ten opzichte van Natura 2000-gebieden; "
        "raadpleeg het ANB voor een passende beoordeling bij twijfel."
    ),
    project_modifier=project_modifier_nature,
)

HERITAGE_INV_DEF = RiskCategoryDef(
    id=RiskCategory.HERITAGE_INV,
    label_nl="Vastgesteld inventaris bouwkundig erfgoed",
    label_en="Protected built heritage inventory",
    severity_prior_days=180,
    base_success_rate=0.50,
    beta_weights={
        "in_protected_heritage": 0.6,
        "heritage_match_distance_m_inv": 0.3,
    },
    evidence_keys=("in_protected_heritage", "heritage_match_distance_m"),
    typical_objector_template_nl="Agentschap Onroerend Erfgoed / erfgoedverenigingen",
    legal_basis_nl="art. 5.4.1 Onroerenderfgoeddecreet; vastgestelde inventaris",
    static_rationale_nl=(
        "Onvoldoende gegevens over erfgoedwaarde of nabijheid tot beschermd erfgoed; "
        "verifieer de vastgestelde inventaris via geo.onroerenderfgoed.be."
    ),
    project_modifier=project_modifier_default,
)

NUISANCE_NOISE_DEF = RiskCategoryDef(
    id=RiskCategory.NUISANCE_NOISE,
    label_nl="Geluid / geur / licht",
    label_en="Noise / odour / light nuisance",
    severity_prior_days=110,
    base_success_rate=0.30,
    beta_weights={
        "iioa_class": 2.0,
        "distance_to_residential_m_inv": 3.0,
    },
    evidence_keys=("iioa_class", "distance_to_residential_m", "project_type"),
    typical_objector_template_nl="omwonenden / OVAM / Departement Omgeving",
    legal_basis_nl="VLAREM II; art. 4.3.1 §2 VCRO hinderaspecten",
    static_rationale_nl=(
        "Onvoldoende gegevens om hinder (geluid, geur, licht) te beoordelen; "
        "verifieer de IIOA-klasse en de afstand tot woonbebouwing."
    ),
    project_modifier=project_modifier_default,
)

PRIVACY_BEZONNING_DEF = RiskCategoryDef(
    id=RiskCategory.PRIVACY_BEZONNING,
    label_nl="Inkijk / bezonning",
    label_en="Privacy / sunlight impact",
    severity_prior_days=100,
    base_success_rate=0.25,
    beta_weights={
        "floors": 0.5,
        "distance_to_residential_m_inv": 3.0,
    },
    evidence_keys=("floors", "distance_to_residential_m"),
    typical_objector_template_nl="directe buren",
    legal_basis_nl="art. 4.3.1 §2 VCRO; woonkwaliteitsnormen",
    static_rationale_nl=(
        "Onvoldoende gegevens over inkijk en bezonningsimpact; verifieer het "
        "aantal bouwlagen en de afstand tot aanpalende woningen."
    ),
    project_modifier=project_modifier_default,
)

BINDING_ADVICE_IGNORED_DEF = RiskCategoryDef(
    id=RiskCategory.BINDING_ADVICE_IGNORED,
    label_nl="Bindend ongunstig advies niet gevolgd",
    label_en="Binding unfavourable advice ignored",
    severity_prior_days=280,
    base_success_rate=0.75,
    beta_weights={
        "mentions_ongunstig_advies": 0.8,
    },
    evidence_keys=("mentions_ongunstig_advies",),
    typical_objector_template_nl="adviserende instantie (ANB, VMM, OE)",
    legal_basis_nl="art. 4.3.3 VCRO; bindend advies conform omgevingsvergunningsdecreet",
    static_rationale_nl=(
        "Onvoldoende tekstinformatie om na te gaan of een bindend ongunstig advies "
        "werd genegeerd; raadpleeg de adviezen in het dossier."
    ),
    project_modifier=project_modifier_default,
)

FUNCTION_MIX_ZONING_DEF = RiskCategoryDef(
    id=RiskCategory.FUNCTION_MIX_ZONING,
    label_nl="Zonevreemd",
    label_en="Zone-incompatible function",
    severity_prior_days=220,
    base_success_rate=0.50,
    beta_weights={
        "rup_zone_present": 0.4,
        "floors": 0.1,
    },
    evidence_keys=("rup_zone", "project_type"),
    typical_objector_template_nl="buurtbewoners / gemeentelijke stedenbouw",
    legal_basis_nl="art. 4.4.1 VCRO zonevreemd gebruik; bestemmingsplan",
    static_rationale_nl=(
        "Onvoldoende gegevens om zonevreemdheid te beoordelen; vergelijk de "
        "bestemming in het BPA/RUP met het opgegeven projecttype."
    ),
    project_modifier=project_modifier_default,
)

VERGUNNINGENCARROUSEL_DEF = RiskCategoryDef(
    id=RiskCategory.VERGUNNINGENCARROUSEL,
    label_nl="Salami-slicing / vergunningencarrousel",
    label_en="Permit splitting (salami slicing)",
    severity_prior_days=200,
    base_success_rate=0.45,
    beta_weights={
        "parcel_repeat_count": 0.5,
    },
    evidence_keys=("parcel_repeat_count",),
    typical_objector_template_nl="Departement Omgeving / omwonenden",
    legal_basis_nl="art. 4.3.1 §2 VCRO; RvVb rechtspraak over opsplitsing",
    static_rationale_nl=(
        "Onvoldoende gegevens over herhaalde aanvragen op hetzelfde perceel; "
        "verifieer via de kadastrale referentie of eerdere dossiers bestaan."
    ),
    project_modifier=project_modifier_carrousel,
)


# ---------------------------------------------------------------------------
# TAXONOMY mapping — must cover all 14 RiskCategory values
# ---------------------------------------------------------------------------

TAXONOMY: Mapping[RiskCategory, RiskCategoryDef] = {
    RiskCategory.GRO_HEIGHT: GRO_HEIGHT_DEF,
    RiskCategory.WATER_FLOOD: WATER_FLOOD_DEF,
    RiskCategory.MER_SCREENING: MER_SCREENING_DEF,
    RiskCategory.BPA_RUP_CONFLICT: BPA_RUP_CONFLICT_DEF,
    RiskCategory.MOTIVATION_DEFECT: MOTIVATION_DEFECT_DEF,
    RiskCategory.TREES_KAPVERG: TREES_KAPVERG_DEF,
    RiskCategory.MOBILITY_PARKING: MOBILITY_PARKING_DEF,
    RiskCategory.NATURE_2000_N: NATURE_2000_N_DEF,
    RiskCategory.HERITAGE_INV: HERITAGE_INV_DEF,
    RiskCategory.NUISANCE_NOISE: NUISANCE_NOISE_DEF,
    RiskCategory.PRIVACY_BEZONNING: PRIVACY_BEZONNING_DEF,
    RiskCategory.BINDING_ADVICE_IGNORED: BINDING_ADVICE_IGNORED_DEF,
    RiskCategory.FUNCTION_MIX_ZONING: FUNCTION_MIX_ZONING_DEF,
    RiskCategory.VERGUNNINGENCARROUSEL: VERGUNNINGENCARROUSEL_DEF,
}


def get_category_def(c: RiskCategory) -> RiskCategoryDef:
    """Return the RiskCategoryDef for the given category; raises KeyError on miss."""
    return TAXONOMY[c]
