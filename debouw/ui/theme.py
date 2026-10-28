"""
UI theme: brand palette, risk colors, Dutch category labels, global CSS, and
logo helpers for the debouw dashboard.

The palette is a light, professional "engineering report" look: petrol-blue
primary, slate text, amber accent, and the established green/amber/red risk
bands. ``GLOBAL_CSS`` is injected once per page via ``inject_global_css`` and
styles the custom HTML cards rendered by ``debouw.ui.components``.
"""

from __future__ import annotations

import base64
from pathlib import Path

from debouw.models.permit import RiskCategory

# ---------------------------------------------------------------------------
# Brand palette
# ---------------------------------------------------------------------------

PRIMARY = "#155e75"  # petrol blue — brand color
PRIMARY_SOFT = "#e8f4f8"  # tinted background for primary chips
ACCENT = "#f59e0b"  # amber accent (matches the mid risk band)
BG = "#f6f8fa"  # app background
SURFACE = "#ffffff"  # card background
BORDER = "#e2e8f0"  # card border
TEXT = "#10212e"  # near-black slate
TEXT_MUTED = "#5b6b7b"  # secondary text

# ---------------------------------------------------------------------------
# Risk colors + bands
# ---------------------------------------------------------------------------

RISK_COLOR_GREEN = "#10b981"
RISK_COLOR_AMBER = "#f59e0b"
RISK_COLOR_RED = "#ef4444"

# Per-band metadata used by chips, cards, and charts. ``color`` is the strong
# accent; ``fg``/``bg`` are a readable text-on-tint pair for pills.
RISK_BANDS: dict[str, dict[str, str]] = {
    "laag": {
        "label": "Laag risico",
        "color": RISK_COLOR_GREEN,
        "fg": "#065f46",
        "bg": "#d1fae5",
    },
    "matig": {
        "label": "Matig risico",
        "color": RISK_COLOR_AMBER,
        "fg": "#92400e",
        "bg": "#fef3c7",
    },
    "hoog": {
        "label": "Hoog risico",
        "color": RISK_COLOR_RED,
        "fg": "#991b1b",
        "bg": "#fee2e2",
    },
}


def band_for_score(score: float) -> str:
    """Return the risk-band key ("laag" | "matig" | "hoog") for a score in [0, 1]."""
    if score < 0.3:
        return "laag"
    if score < 0.6:
        return "matig"
    return "hoog"


def band_info(score: float) -> dict[str, str]:
    """Return the band metadata dict (label/color/fg/bg) for a score."""
    return RISK_BANDS[band_for_score(score)]


def color_for_score(score: float) -> str:
    """Return a hex color for a risk score in [0, 1]."""
    return RISK_BANDS[band_for_score(score)]["color"]


# ---------------------------------------------------------------------------
# Dutch labels
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

LOGO_PATH = Path(__file__).parent / "assets" / "logo.svg"


def logo_data_uri() -> str:
    """Return the logo SVG as a base64 data URI for inline ``<img>`` embedding."""
    encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# ---------------------------------------------------------------------------
# Global CSS
# ---------------------------------------------------------------------------

GLOBAL_CSS = f"""
/* ---- layout ------------------------------------------------------------ */
div.block-container {{
    padding-top: 4rem;
    padding-bottom: 2.5rem;
    max-width: 1440px;
}}
header[data-testid="stHeader"] {{
    background: rgba(246, 248, 250, 0.85);
    backdrop-filter: blur(4px);
}}

/* ---- header bar --------------------------------------------------------- */
.db-header {{
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 18px;
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(16, 33, 46, 0.05);
    margin-bottom: 8px;
}}
.db-header img {{
    height: 32px;
    display: block;
}}
.db-header-title {{
    font-size: 1.02rem;
    font-weight: 700;
    color: {TEXT};
    line-height: 1.2;
}}
.db-header-sub {{
    font-size: 0.78rem;
    color: {TEXT_MUTED};
}}
.db-header-spacer {{
    flex: 1;
}}
.db-badge {{
    font-size: 0.72rem;
    font-weight: 600;
    color: {PRIMARY};
    background: {PRIMARY_SOFT};
    border: 1px solid #cde5ee;
    border-radius: 999px;
    padding: 3px 10px;
    white-space: nowrap;
}}
.db-badge.db-badge-muted {{
    color: {TEXT_MUTED};
    background: #f1f5f9;
    border-color: {BORDER};
}}

/* ---- section titles ------------------------------------------------------ */
.db-section-title {{
    font-size: 0.78rem;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: {PRIMARY};
    margin: 18px 0 8px 0;
}}

/* ---- metric cards -------------------------------------------------------- */
.db-metric-row {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
    gap: 12px;
    margin: 4px 0 10px 0;
}}
.db-metric-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-left: 4px solid var(--accent, {PRIMARY});
    border-radius: 12px;
    padding: 14px 16px 12px 16px;
    box-shadow: 0 1px 2px rgba(16, 33, 46, 0.04);
    transition: box-shadow 0.15s ease, transform 0.15s ease;
}}
.db-metric-card:hover {{
    box-shadow: 0 4px 14px rgba(16, 33, 46, 0.1);
    transform: translateY(-1px);
}}
.db-metric-label {{
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: {TEXT_MUTED};
    margin-bottom: 6px;
    white-space: nowrap;
}}
.db-metric-value {{
    font-size: 1.75rem;
    font-weight: 800;
    color: {TEXT};
    line-height: 1.05;
    font-variant-numeric: tabular-nums;
}}
.db-metric-unit {{
    font-size: 0.95rem;
    font-weight: 600;
    color: {TEXT_MUTED};
    margin-left: 3px;
}}
.db-metric-sub {{
    margin-top: 8px;
    font-size: 0.74rem;
    color: {TEXT_MUTED};
}}
.db-metric-pill {{
    display: inline-block;
    margin-top: 8px;
    font-size: 0.72rem;
    font-weight: 700;
    border-radius: 999px;
    padding: 2px 10px;
}}
.db-meter {{
    margin-top: 9px;
    height: 6px;
    border-radius: 999px;
    background: #eef2f6;
    overflow: hidden;
}}
.db-meter > span {{
    display: block;
    height: 100%;
    border-radius: 999px;
    background: var(--accent, {PRIMARY});
}}

/* ---- risk factor cards ---------------------------------------------------- */
.db-risk-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-left: 4px solid var(--band, #94a3b8);
    border-radius: 12px;
    padding: 13px 16px;
    margin-bottom: 10px;
    box-shadow: 0 1px 2px rgba(16, 33, 46, 0.04);
    transition: box-shadow 0.15s ease, transform 0.15s ease;
}}
.db-risk-card:hover {{
    box-shadow: 0 4px 14px rgba(16, 33, 46, 0.1);
    transform: translateY(-1px);
}}
.db-risk-head {{
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
}}
.db-risk-rank {{
    font-size: 0.72rem;
    font-weight: 800;
    color: {TEXT_MUTED};
    background: #f1f5f9;
    border-radius: 8px;
    padding: 2px 8px;
}}
.db-risk-title {{
    font-weight: 700;
    color: {TEXT};
    font-size: 0.95rem;
}}
.db-risk-stats {{
    display: flex;
    gap: 20px;
    margin: 9px 0 2px 0;
    flex-wrap: wrap;
}}
.db-risk-stat {{
    font-size: 0.76rem;
    color: {TEXT_MUTED};
}}
.db-risk-stat b {{
    display: block;
    color: {TEXT};
    font-size: 0.95rem;
    font-variant-numeric: tabular-nums;
}}
.db-risk-rationale {{
    font-size: 0.84rem;
    color: #33444f;
    margin-top: 7px;
    line-height: 1.45;
}}
.db-risk-meta {{
    font-size: 0.74rem;
    color: {TEXT_MUTED};
    margin-top: 7px;
}}
.db-chip {{
    display: inline-block;
    font-size: 0.7rem;
    font-weight: 700;
    border-radius: 999px;
    padding: 2px 9px;
    white-space: nowrap;
}}

/* ---- dossier header + panels ---------------------------------------------- */
.db-dossier-title {{
    font-size: 1.3rem;
    font-weight: 800;
    color: {TEXT};
    line-height: 1.25;
    margin-bottom: 2px;
}}
.db-dossier-sub {{
    font-size: 0.86rem;
    color: {TEXT_MUTED};
    margin-bottom: 8px;
}}
.db-chip-row {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 4px;
}}
.db-panel {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
    padding: 15px 18px;
    box-shadow: 0 1px 2px rgba(16, 33, 46, 0.04);
}}
.db-panel-title {{
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: {TEXT_MUTED};
    margin-bottom: 8px;
}}
.db-panel-body {{
    font-size: 0.88rem;
    color: #33444f;
    line-height: 1.55;
}}

/* ---- precedent cards ------------------------------------------------------- */
.db-precedent {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 11px 14px;
    margin-bottom: 8px;
}}
.db-precedent a {{
    color: {PRIMARY};
    font-weight: 700;
    text-decoration: none;
}}
.db-precedent a:hover {{
    text-decoration: underline;
}}
.db-precedent-summary {{
    font-size: 0.8rem;
    color: #33444f;
    margin-top: 6px;
    line-height: 1.45;
}}

/* ---- streamlit widget polish ------------------------------------------------ */
details[data-testid="stExpander"] {{
    border-radius: 12px;
}}
div[data-testid="stExpander"] summary {{
    font-size: 0.88rem;
}}
"""


def inject_global_css() -> None:
    """Inject the global stylesheet once per rerun."""
    import streamlit as st

    st.markdown(f"<style>{GLOBAL_CSS}</style>", unsafe_allow_html=True)
