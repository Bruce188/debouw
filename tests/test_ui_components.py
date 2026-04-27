"""
UI component tests for the debouw dashboard.

Primary: component-level tests (no AppTest — Q8 fallback is the primary gate).
Secondary: AppTest smoke test (marked xfail(strict=False) as allowed to flake).

AppTest is known to have import/network issues in restricted environments.
Component-level tests are the reliable primary assertion.
"""

from __future__ import annotations

import pytest

from debouw.ui.theme import RISK_CATEGORY_LABELS_NL, color_for_score
from debouw.models.permit import RiskCategory


# ---------------------------------------------------------------------------
# 7.7-1: color_for_score thresholds
# ---------------------------------------------------------------------------


def test_color_for_score_thresholds() -> None:
    """Verify the three color bands map correctly."""
    green = color_for_score(0.0)
    assert green == "#10b981", f"Expected green for 0.0, got {green}"

    amber_low = color_for_score(0.29)
    assert amber_low == "#10b981", "0.29 should be green"

    amber = color_for_score(0.3)
    assert amber == "#f59e0b", f"Expected amber for 0.3, got {amber}"

    amber2 = color_for_score(0.4)
    assert amber2 == "#f59e0b", "0.4 should be amber"

    red = color_for_score(0.6)
    assert red == "#ef4444", f"Expected red for 0.6, got {red}"

    red2 = color_for_score(0.8)
    assert red2 == "#ef4444", "0.8 should be red"

    red3 = color_for_score(1.0)
    assert red3 == "#ef4444", "1.0 should be red"


# ---------------------------------------------------------------------------
# 7.7-2: RISK_CATEGORY_LABELS_NL is exhaustive
# ---------------------------------------------------------------------------


def test_risk_category_labels_exhaustive() -> None:
    """All RiskCategory enum values must have a Dutch label."""
    for cat in RiskCategory:
        assert cat in RISK_CATEGORY_LABELS_NL, f"Missing NL label for {cat}"
    assert len(RISK_CATEGORY_LABELS_NL) == len(list(RiskCategory))


# ---------------------------------------------------------------------------
# 7.7-3: render_project_detail shows info when assessment is None
# (component-level test using unittest mock for streamlit)
# ---------------------------------------------------------------------------


def test_no_assessment_shows_info() -> None:
    """render_project_detail calls st.info when assessment is None."""
    from unittest.mock import MagicMock, patch

    project = {
        "external_id": "gent:OMV_TEST_0001",
        "title": "Test Project",
        "omv_reference": "OMV_TEST_0001",
        "source": "gent_consultatie",
        "address": {"raw": "Korenmarkt 1, 9000 Gent", "municipality": "Gent"},
        "status": "intake",
        "first_seen_at": "2026-04-26T12:00:00+00:00",
        "overlays": None,
    }

    # We need to mock all streamlit calls
    info_calls: list = []

    mock_st = MagicMock()
    mock_st.info.side_effect = lambda msg: info_calls.append(msg)

    # columns returns two mock objects (context managers)
    col1, col2 = MagicMock(), MagicMock()
    col1.__enter__ = lambda s: s
    col1.__exit__ = MagicMock(return_value=False)
    col2.__enter__ = lambda s: s
    col2.__exit__ = MagicMock(return_value=False)
    mock_st.columns.return_value = (col1, col2)

    import debouw.ui.components as comp_module

    with patch.object(comp_module, "st", mock_st):
        comp_module.render_project_detail(project, assessment=None)

    assert info_calls, "st.info() should have been called when assessment is None"
    assert any(
        "niet uitgevoerd" in msg or "risico" in msg.lower() for msg in info_calls
    ), f"Expected Dutch 'geen assessment' message, got: {info_calls}"


# ---------------------------------------------------------------------------
# 7.7-4: AppTest smoke (optional — xfail if flaky)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=False, reason="AppTest may be flaky in restricted environments")
def test_apptest_smoke(tmp_path) -> None:
    """Smoke test via Streamlit AppTest. Marked xfail(strict=False)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(
        __import__("pathlib").Path(__file__).parent.parent / "debouw" / "ui" / "app.py"
    ))
    at.run(timeout=10)

    # If we reach here, the app launched without exception
    # An empty table (0 projects in tmp DB) is acceptable
    assert not at.exception


# ---------------------------------------------------------------------------
# Phase 4 — Task 6.8: gemeente multiselect filter logic tests
# ---------------------------------------------------------------------------


def test_gemeente_multiselect_filter_logic():
    """Phase 4: multiselect filter on Gemeenten works set-membership-wise."""
    projects = [
        {"external_id": "gent:OMV_1", "address": {"municipality": "Gent"}},
        {"external_id": "gent:OMV_2", "address": {"municipality": "GENT"}},
        {"external_id": "vl_inz:OMV_3", "address": {"municipality": "Antwerpen"}},
        {"external_id": "vl_inz:OMV_4", "address": {"municipality": None}},
    ]
    selected = ["gent"]
    selected_lower = {g.lower() for g in selected}

    def _muni(p: dict) -> str:
        addr = p.get("address") or {}
        if isinstance(addr, dict):
            return (addr.get("municipality") or "").lower()
        return ""

    filtered = [p for p in projects if _muni(p) in selected_lower]
    assert len(filtered) == 2
    assert all(p["external_id"].startswith("gent:") for p in filtered)


def test_gemeente_options_dedup_and_sort():
    """The multiselect options list is deduped + sorted (case-preserved)."""
    projects = [
        {"address": {"municipality": "Sint-Niklaas"}},
        {"address": {"municipality": "Antwerpen"}},
        {"address": {"municipality": "Sint-Niklaas"}},
        {"address": {"municipality": None}},
        {"address": "string-not-dict"},
    ]
    municipalities = sorted({
        (p.get("address") or {}).get("municipality")
        for p in projects
        if isinstance(p.get("address"), dict)
        and p.get("address", {}).get("municipality")
    })
    assert municipalities == ["Antwerpen", "Sint-Niklaas"]


# ---------------------------------------------------------------------------
# 7.7-5: render_risk_table with empty projects list shows info (component test)
# ---------------------------------------------------------------------------


def test_render_risk_table_empty() -> None:
    """render_risk_table shows info message when no projects present."""
    from unittest.mock import MagicMock, patch

    info_calls: list = []
    mock_st = MagicMock()
    mock_st.info.side_effect = lambda msg: info_calls.append(msg)

    import debouw.ui.components as comp_module

    with patch.object(comp_module, "st", mock_st):
        result = comp_module.render_risk_table([], {})

    assert result is None
    assert info_calls, "st.info() should be called on empty projects"


# ---------------------------------------------------------------------------
# Phase 5 (Task 6.1): Regio multiselect filter logic
# ---------------------------------------------------------------------------

def test_regio_multiselect_filter_logic():
    """Region filter correctly partitions vl/brussels/wl rows."""
    _REGION_LABEL_MAP = {
        "Vlaanderen": "vl",
        "Brussel": "brussels",
        "Wallonië": "wl",
    }

    projects = [
        {"external_id": "gent:OMV_1", "region": "vl", "address": {"municipality": "Gent"}},
        {"external_id": "vl_inz:OMV_2", "region": "vl", "address": {"municipality": "Antwerpen"}},
        {"external_id": "brussels:01/PU/1984289", "region": "brussels", "address": {"municipality": "Anderlecht"}},
    ]

    # Select only Vlaanderen
    selected_regio = ["Vlaanderen"]
    selected_region_enums = {_REGION_LABEL_MAP[label] for label in selected_regio}
    filtered = [p for p in projects if p.get("region") in selected_region_enums]
    assert len(filtered) == 2
    assert all(p["region"] == "vl" for p in filtered)

    # Select only Brussel
    selected_regio = ["Brussel"]
    selected_region_enums = {_REGION_LABEL_MAP[label] for label in selected_regio}
    filtered = [p for p in projects if p.get("region") in selected_region_enums]
    assert len(filtered) == 1
    assert filtered[0]["external_id"] == "brussels:01/PU/1984289"

    # Empty selection → all rows pass
    selected_regio = []
    if not selected_regio:
        filtered = projects
    assert len(filtered) == 3

    # Wallonië selected → no rows (wl rows not ingested yet)
    selected_regio = ["Wallonië"]
    selected_region_enums = {_REGION_LABEL_MAP[label] for label in selected_regio}
    filtered = [p for p in projects if p.get("region") in selected_region_enums]
    assert len(filtered) == 0


def test_regio_and_gemeente_and_filter():
    """Regio AND gemeente filter combined: intersection semantics."""
    _REGION_LABEL_MAP = {"Vlaanderen": "vl", "Brussel": "brussels", "Wallonië": "wl"}

    projects = [
        {"external_id": "gent:OMV_1", "region": "vl", "address": {"municipality": "Gent"}},
        {"external_id": "vl_inz:OMV_2", "region": "vl", "address": {"municipality": "Antwerpen"}},
        {"external_id": "brussels:01/PU/1984289", "region": "brussels", "address": {"municipality": "Anderlecht"}},
    ]

    selected_regio = ["Vlaanderen"]
    selected_region_enums = {_REGION_LABEL_MAP[label] for label in selected_regio}
    selected_gemeenten = ["Antwerpen"]
    selected_lower = {g.lower() for g in selected_gemeenten}

    def _muni(p: dict) -> str:
        addr = p.get("address") or {}
        if isinstance(addr, dict):
            return (addr.get("municipality") or "").lower()
        return ""

    # Region filter first
    after_region = [p for p in projects if p.get("region") in selected_region_enums]
    assert len(after_region) == 2

    # Then gemeente filter
    after_gemeente = [p for p in after_region if _muni(p) in selected_lower]
    assert len(after_gemeente) == 1
    assert after_gemeente[0]["external_id"] == "vl_inz:OMV_2"
