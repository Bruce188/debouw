"""
UI theme: risk colors and Dutch category labels.
"""

from __future__ import annotations

from debouw.models.permit import RiskCategory

RISK_COLOR_GREEN = "#10b981"
RISK_COLOR_AMBER = "#f59e0b"
RISK_COLOR_RED = "#ef4444"


def color_for_score(score: float) -> str:
    """Return a hex color for a risk score in [0, 1]."""
    if score < 0.3:
        return RISK_COLOR_GREEN
    if score < 0.6:
        return RISK_COLOR_AMBER
    return RISK_COLOR_RED


RISK_CATEGORY_LABELS_NL: dict[RiskCategory, str] = {
    RiskCategory.GRO_HEIGHT: "Schaal & bouwhoogte",
    RiskCategory.WATER_FLOOD: "Watertoets / overstromingsgevoelig",
    RiskCategory.MER_SCREENING: "Project-MER ontoereikend",
    RiskCategory.BPA_RUP_CONFLICT: "Strijdig met BPA/RUP",
    RiskCategory.MOTIVATION_DEFECT: "Onafdoende motivering",
    RiskCategory.TREES_KAPVERG: "Kapvergunning waardevolle bomen",
    RiskCategory.MOBILITY_PARKING: "Parkeernorm tekort",
    RiskCategory.NATURE_2000_N: "Passende beoordeling / stikstof",
    RiskCategory.HERITAGE_INV: "Vastgesteld erfgoed",
    RiskCategory.NUISANCE_NOISE: "Geluid / geur / licht",
    RiskCategory.PRIVACY_BEZONNING: "Inkijk / bezonning",
    RiskCategory.BINDING_ADVICE_IGNORED: "Bindend advies genegeerd",
    RiskCategory.FUNCTION_MIX_ZONING: "Zonevreemd",
    RiskCategory.VERGUNNINGENCARROUSEL: "Vergunningencarrousel",
}
