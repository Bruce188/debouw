"""
Theme tests: palette consistency, risk bands, logo asset, and global CSS.
"""

from __future__ import annotations

import base64

from debouw.models.permit import RiskCategory
from debouw.ui.theme import (
    GLOBAL_CSS,
    LOGO_PATH,
    RISK_BANDS,
    RISK_CATEGORY_LABELS_NL,
    RISK_COLOR_AMBER,
    RISK_COLOR_GREEN,
    RISK_COLOR_RED,
    band_for_score,
    band_info,
    color_for_score,
    logo_data_uri,
)


# ---------------------------------------------------------------------------
# Risk bands
# ---------------------------------------------------------------------------


def test_band_for_score_thresholds() -> None:
    assert band_for_score(0.0) == "laag"
    assert band_for_score(0.29) == "laag"
    assert band_for_score(0.3) == "matig"
    assert band_for_score(0.59) == "matig"
    assert band_for_score(0.6) == "hoog"
    assert band_for_score(1.0) == "hoog"


def test_color_for_score_matches_bands() -> None:
    """Legacy color_for_score stays consistent with the band metadata."""
    assert color_for_score(0.1) == RISK_COLOR_GREEN == RISK_BANDS["laag"]["color"]
    assert color_for_score(0.4) == RISK_COLOR_AMBER == RISK_BANDS["matig"]["color"]
    assert color_for_score(0.9) == RISK_COLOR_RED == RISK_BANDS["hoog"]["color"]


def test_band_info_shape() -> None:
    for score, key in ((0.05, "laag"), (0.45, "matig"), (0.95, "hoog")):
        info = band_info(score)
        assert info is RISK_BANDS[key]
        for field in ("label", "color", "fg", "bg"):
            assert info[field].strip(), f"band {key} missing {field}"
        assert info["color"].startswith("#")


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_risk_category_labels_exhaustive() -> None:
    for cat in RiskCategory:
        assert cat in RISK_CATEGORY_LABELS_NL, f"Missing NL label for {cat}"
    assert len(RISK_CATEGORY_LABELS_NL) == len(list(RiskCategory))


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------


def test_logo_asset_exists() -> None:
    assert LOGO_PATH.exists(), f"logo missing at {LOGO_PATH}"
    svg = LOGO_PATH.read_text(encoding="utf-8")
    assert "<svg" in svg
    assert "debouw" in svg


def test_logo_data_uri_roundtrip() -> None:
    uri = logo_data_uri()
    prefix = "data:image/svg+xml;base64,"
    assert uri.startswith(prefix)
    decoded = base64.b64decode(uri[len(prefix):]).decode("utf-8")
    assert "<svg" in decoded


# ---------------------------------------------------------------------------
# Global CSS
# ---------------------------------------------------------------------------


def test_global_css_contains_component_classes() -> None:
    for cls in (
        ".db-header",
        ".db-badge",
        ".db-metric-row",
        ".db-metric-card",
        ".db-risk-card",
        ".db-chip",
        ".db-meter",
        ".db-panel",
        ".db-precedent",
        ".db-section-title",
    ):
        assert cls in GLOBAL_CSS, f"GLOBAL_CSS missing {cls}"


def test_global_css_has_hover_states() -> None:
    assert ".db-metric-card:hover" in GLOBAL_CSS
    assert ".db-risk-card:hover" in GLOBAL_CSS
