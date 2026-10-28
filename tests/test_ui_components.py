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

    # columns returns one context-manager MagicMock per column in the spec,
    # regardless of whether a list or int is passed.  This makes the mock
    # resilient to future column-count changes.
    def _make_col() -> MagicMock:
        col = MagicMock()
        col.__enter__ = lambda s: s
        col.__exit__ = MagicMock(return_value=False)
        return col

    def _columns_side_effect(spec, **kwargs):
        n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
        return tuple(_make_col() for _ in range(n))

    mock_st.columns.side_effect = _columns_side_effect

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
    """Region filter correctly partitions vl/brussels rows."""
    _REGION_LABEL_MAP = {
        "Vlaanderen": "vl",
        "Brussel": "brussels",
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


def test_regio_and_gemeente_and_filter():
    """Regio AND gemeente filter combined: intersection semantics."""
    _REGION_LABEL_MAP = {"Vlaanderen": "vl", "Brussel": "brussels"}

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


# ---------------------------------------------------------------------------
# Component copy + rendering behavior
# ---------------------------------------------------------------------------


def _mock_st():
    """MagicMock for the streamlit module with working st.columns()."""
    from unittest.mock import MagicMock

    mock_st = MagicMock()

    def _make_col() -> MagicMock:
        col = MagicMock()
        col.__enter__ = lambda s: s
        col.__exit__ = MagicMock(return_value=False)
        return col

    def _columns_side_effect(spec, **kwargs):
        n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
        return tuple(_make_col() for _ in range(n))

    mock_st.columns.side_effect = _columns_side_effect
    return mock_st


def test_render_risk_table_empty_with_filters_explains_why() -> None:
    """With active filters, the empty-table message says how to recover."""
    from unittest.mock import patch

    mock_st = _mock_st()
    info_calls: list = []
    mock_st.info.side_effect = lambda msg: info_calls.append(msg)

    import debouw.ui.components as comp_module

    with patch.object(comp_module, "st", mock_st):
        result = comp_module.render_risk_table([], {}, filters_active=True)

    assert result is None
    assert any("filters" in msg for msg in info_calls), info_calls
    assert any("minimumscore" in msg for msg in info_calls), info_calls


def test_render_risk_table_handles_null_title() -> None:
    """A SQL NULL title must not crash the Adres fallback."""
    from unittest.mock import patch

    mock_st = _mock_st()
    mock_st.selectbox.return_value = ""
    mock_st.session_state = {}

    import debouw.ui.components as comp_module

    projects = [
        {
            "external_id": "brussels:01/PU/1",
            "omv_reference": "01/PU/1",
            "title": None,
            "address": None,
            "status": "intake",
        }
    ]
    with patch.object(comp_module, "st", mock_st):
        result = comp_module.render_risk_table(projects, {})

    assert result is None


def test_precedent_links_use_dbrc_search_url() -> None:
    """Arrest links point at the dbrc.be search page, not a per-arrest path."""
    from unittest.mock import patch

    mock_st = _mock_st()
    md_calls: list = []
    mock_st.markdown.side_effect = lambda msg, **kw: md_calls.append(str(msg))

    import debouw.ui.components as comp_module

    top_risks = [
        {
            "category": "mer_screening",
            "precedents": [
                {
                    "precedent_id": "RVVB.A.2223.1006",
                    "outcome": "vernietigd",
                    "similarity": 0.82,
                    "summary": "Testarrest",
                }
            ],
        }
    ]
    with patch.object(comp_module, "st", mock_st):
        comp_module._render_precedent_block(top_risks)

    joined = "\n".join(md_calls)
    assert "search_api_fulltext=RVVB-A-2223-1006" in joined
    assert "/rechtspraak/arrest/" not in joined


def test_low_score_caption_copy() -> None:
    """The low-score caption is confident, neutral Dutch without rule jargon."""
    from unittest.mock import patch

    mock_st = _mock_st()
    captions: list = []
    mock_st.caption.side_effect = lambda msg: captions.append(msg)

    import debouw.ui.components as comp_module

    with patch.object(comp_module, "st", mock_st):
        comp_module._render_metrics(
            {"overall_score": 0.08, "expected_delay_days": 121.0, "confidence": 0.48}
        )

    assert captions, "expected a caption for the low-score band"
    assert "Laag risicoprofiel" in captions[0]
    assert "regels" not in captions[0]
    assert "Basislijn" not in captions[0]


def test_empty_risk_factors_message() -> None:
    """Empty top_risks renders a friendly Dutch message without jargon."""
    from unittest.mock import patch

    mock_st = _mock_st()
    info_calls: list = []
    mock_st.info.side_effect = lambda msg: info_calls.append(msg)

    import debouw.ui.components as comp_module

    with patch.object(comp_module, "st", mock_st):
        comp_module.render_risk_factors([])

    assert any("Nog geen risicofactoren" in msg for msg in info_calls), info_calls
    assert all("fallback" not in msg for msg in info_calls), info_calls


def test_first_seen_none_renders_dash() -> None:
    """first_seen_at = NULL renders as an em-dash, not the string 'None'."""
    from unittest.mock import patch

    mock_st = _mock_st()
    md_calls: list = []
    mock_st.markdown.side_effect = lambda msg, **kw: md_calls.append(str(msg))

    import debouw.ui.components as comp_module

    project = {
        "external_id": "brussels:01/PU/2",
        "title": "Testdossier",
        "omv_reference": "01/PU/2",
        "address": {"raw": "Teststraat 1, 1000 Brussel", "municipality": "Brussel"},
        "status": "intake",
        "first_seen_at": None,
        "overlays": None,
    }
    with patch.object(comp_module, "st", mock_st):
        comp_module.render_project_detail(project, assessment=None)

    aanmelding = [m for m in md_calls if "Eerste aanmelding" in m]
    assert aanmelding, md_calls
    assert "None" not in aanmelding[0]
    assert "—" in aanmelding[0]


def test_clear_risk_table_selection_resets_widgets() -> None:
    """clear_risk_table_selection bumps the table key and resets the picker."""
    from unittest.mock import MagicMock, patch

    mock_st = MagicMock()
    state = {
        "_risk_table_gen": 1,
        "dossier_picker": "brussels:01/PU/3",
        "_risk_table_row_choice": "brussels:01/PU/3",
        "_risk_table_sb_choice": None,
    }
    mock_st.session_state = state

    import debouw.ui.components as comp_module

    with patch.object(comp_module, "st", mock_st):
        comp_module.clear_risk_table_selection()

    assert state["_risk_table_gen"] == 2
    assert state["dossier_picker"] == ""
    assert "_risk_table_row_choice" not in state
    assert "_risk_table_sb_choice" not in state


# ---------------------------------------------------------------------------
# Investor polish: risk-factor ranking + show-all split
# ---------------------------------------------------------------------------


def _mk_rf(cat: str = "gro_height", prob: float = 0.5, sev: float = 0.5, **kw) -> dict:
    rf = {
        "category": cat,
        "probability": prob,
        "severity": sev,
        "expected_delay_days": 30,
        "rationale": "testtoelichting",
        "typical_objector": "buurtcomité",
    }
    rf.update(kw)
    return rf


def test_split_risks_many_factors():
    """Engine 0.7.0 rows (all triggered factors): 5 primary + remainder."""
    from debouw.ui.components import PRIMARY_RISK_COUNT, _split_risks

    risks = [_mk_rf(prob=0.05 * i, sev=1.0) for i in range(1, 13)]  # 12 factors
    primary, rest = _split_risks(risks)
    assert len(primary) == PRIMARY_RISK_COUNT
    assert len(rest) == 12 - PRIMARY_RISK_COUNT

    # Ranked by impact (probability x severity) descending across the split.
    impacts = [r["probability"] * r["severity"] for r in primary + rest]
    assert impacts == sorted(impacts, reverse=True)


def test_split_risks_few_factors():
    """Engine 0.3.0 rows (<=5 factors): everything primary, nothing hidden."""
    from debouw.ui.components import _split_risks

    risks = [_mk_rf() for _ in range(3)]
    primary, rest = _split_risks(risks)
    assert len(primary) == 3
    assert rest == []


def test_render_risk_factors_uses_expander_for_remainder():
    """>5 factors → 'Toon alle N risicofactoren' expander holds the rest."""
    from unittest.mock import MagicMock, patch

    import debouw.ui.components as comp_module

    mock_st = MagicMock()
    risks = [_mk_rf(prob=0.05 * i) for i in range(1, 13)]  # 12 factors

    with patch.object(comp_module, "st", mock_st):
        comp_module.render_risk_factors(risks)

    assert mock_st.expander.called, "expander expected for the remainder"
    label = mock_st.expander.call_args[0][0]
    assert "Toon alle 12 risicofactoren" in label

    # 5 primary cards rendered outside the expander, 7 inside (12 markdown
    # calls total for cards; the expander body re-uses st.markdown too).
    card_calls = [
        c for c in mock_st.markdown.call_args_list if "db-risk-card" in str(c)
    ]
    assert len(card_calls) == 12


def test_render_risk_factors_no_expander_when_few():
    """<=5 factors → all cards inline, no expander."""
    from unittest.mock import MagicMock, patch

    import debouw.ui.components as comp_module

    mock_st = MagicMock()
    with patch.object(comp_module, "st", mock_st):
        comp_module.render_risk_factors([_mk_rf() for _ in range(3)])

    assert not mock_st.expander.called


def test_render_risk_factors_empty_shows_info():
    from unittest.mock import MagicMock, patch

    import debouw.ui.components as comp_module

    mock_st = MagicMock()
    with patch.object(comp_module, "st", mock_st):
        comp_module.render_risk_factors([])

    assert mock_st.info.called


# ---------------------------------------------------------------------------
# Investor polish: metric cards
# ---------------------------------------------------------------------------


def test_metric_cards_html_visually_separated():
    """Each metric renders as its own card with label, value, and unit."""
    from debouw.ui.components import _metric_cards_html

    html = _metric_cards_html(
        {"overall_score": 0.4, "expected_delay_days": 158.2, "confidence": 0.48},
        n_factors=12,
    )
    assert html.count("db-metric-card") == 4
    assert "Risicoscore" in html
    assert "Verwachte vertraging" in html
    assert "158" in html and "dagen" in html
    assert "Betrouwbaarheid" in html and "48" in html
    assert "Matig risico" in html  # band pill for score 0.4
    assert "Risicofactoren" in html and ">12<" in html


def test_metric_cards_html_without_factor_count():
    from debouw.ui.components import _metric_cards_html

    html = _metric_cards_html(
        {"overall_score": 0.08, "expected_delay_days": 121, "confidence": 0.48}
    )
    assert html.count("db-metric-card") == 3
    assert "Laag risico" in html


# ---------------------------------------------------------------------------
# Investor polish: risk cards (content + escaping)
# ---------------------------------------------------------------------------


def test_risk_card_html_escapes_markup():
    from debouw.ui.components import _risk_card_html

    rf = _mk_rf(rationale="<script>alert(1)</script>")
    out = _risk_card_html(rf, 1)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_risk_card_prefers_engine_label():
    """Engine 0.7.0 ships a Dutch per-factor label — it wins over the map."""
    from debouw.ui.components import _risk_card_html

    rf = _mk_rf(
        cat="binding_advice_ignored",
        label="Bindend ongunstig advies niet gevolgd",
    )
    out = _risk_card_html(rf, 1)
    assert "Bindend ongunstig advies niet gevolgd" in out


def test_risk_card_category_fallbacks():
    from debouw.ui.components import _risk_card_html

    # Known category without label → NL category label
    out = _risk_card_html(_mk_rf(cat="gro_height"), 1)
    assert "Schaal &amp; bouwhoogte" in out

    # Unknown category → raw string passes through (no crash)
    out = _risk_card_html(_mk_rf(cat="brand_new_category"), 2)
    assert "brand_new_category" in out


def test_risk_card_contains_probability_and_rationale():
    from debouw.ui.components import _risk_card_html

    out = _risk_card_html(_mk_rf(prob=0.42), 3)
    assert "42%" in out
    assert "testtoelichting" in out
    assert "buurtcomité" in out
    assert "#3" in out


# ---------------------------------------------------------------------------
# Investor polish: charts
# ---------------------------------------------------------------------------


def test_category_breakdown_chart_builds():
    from debouw.ui.components import _category_breakdown_chart

    chart = _category_breakdown_chart(
        [_mk_rf(), _mk_rf(cat="water_flood", prob=0.2)]
    )
    assert chart is not None
    assert _category_breakdown_chart([]) is None


def test_score_distribution_chart_builds():
    from debouw.ui.components import _score_distribution_chart

    chart = _score_distribution_chart([0.1, 0.2, 0.5, 0.7], current=0.4)
    assert chart is not None
    assert _score_distribution_chart([]) is None
    # current=None still renders the histogram
    assert _score_distribution_chart([0.3]) is not None


def test_score_distribution_chart_pins_scale_domains():
    """UAT-005: bin extent and x/y scale domains are pinned in the spec.

    Data-driven domains are computed over an empty dataset during Streamlit's
    two-phase render and spam "Infinite extent" warnings to the JS console.
    """
    from debouw.ui.components import _score_distribution_chart

    spec = _score_distribution_chart(
        [0.07, 0.075, 0.075, 0.4], current=0.4
    ).to_dict()
    bars_enc = spec["layer"][0]["encoding"]
    assert bars_enc["x"]["bin"]["extent"] == [0, 1]
    assert bars_enc["x"]["scale"]["domain"] == [0, 1]
    # y-max equals the largest bin count (two scores share the 0.05-0.10 bin
    # with 0.07, so that bin holds 3).
    assert bars_enc["y"]["scale"]["domain"] == [0, 3]


# ---------------------------------------------------------------------------
# Investor polish: header + portfolio summary
# ---------------------------------------------------------------------------


def test_render_header_embeds_logo_and_engine_badge():
    from unittest.mock import MagicMock, patch

    import debouw.ui.components as comp_module

    mock_st = MagicMock()
    with patch.object(comp_module, "st", mock_st):
        comp_module.render_header("0.6.0")

    assert mock_st.markdown.called
    html = mock_st.markdown.call_args[0][0]
    kwargs = mock_st.markdown.call_args[1]
    assert "data:image/svg+xml;base64," in html
    assert "Engine 0.6.0" in html
    assert kwargs.get("unsafe_allow_html") is True


def test_render_header_contains_visually_hidden_brand_text():
    """UAT-001: 'debouw' must exist as body text, not only as <img alt>."""
    from unittest.mock import MagicMock, patch

    import debouw.ui.components as comp_module

    mock_st = MagicMock()
    with patch.object(comp_module, "st", mock_st):
        comp_module.render_header("0.7.0")

    html = mock_st.markdown.call_args[0][0]
    assert ">debouw</span>" in html
    assert "left:-10000px" in html


# ---------------------------------------------------------------------------
# UAT-004: narration-language detection + header chip
# ---------------------------------------------------------------------------


def test_summary_language_prefers_case_language_field():
    from debouw.ui.components import _summary_language

    assert _summary_language({"case_language": "fr"}, "") == "fr"
    assert _summary_language({"case_language": "nl"}, "Le projet est grand") == "nl"


def test_summary_language_french_heuristic():
    from debouw.ui.components import _summary_language

    fr = (
        "Le dossier présente un risque modéré principalement lié à "
        "l'abattage des arbres et la mobilité dans le quartier."
    )
    assert _summary_language({}, fr) == "fr"


def test_summary_language_dutch_heuristic():
    from debouw.ui.components import _summary_language

    nl = (
        "Het dossier vertoont een matig risico, voornamelijk door het "
        "kappen van waardevolle bomen en de parkeernorm in deze buurt."
    )
    assert _summary_language({}, nl) == "nl"


def test_summary_language_undeterminable_returns_none():
    from debouw.ui.components import _summary_language

    assert _summary_language({}, "") is None
    assert _summary_language({"case_language": None}, "123 456") is None


def test_dossier_header_renders_language_chip():
    from debouw.ui.components import _dossier_header_html

    project = {
        "external_id": "brussels:16/PU/2028242",
        "title": "Avenue de la Chênaie 160",
        "status": "intake",
        "case_language": "fr",
    }
    html = _dossier_header_html(project, {"summary": "Le projet est risqué"})
    assert "Taal: FR" in html

    # No language signal at all → no chip
    html = _dossier_header_html({"title": "x", "status": "intake"}, None)
    assert "Taal:" not in html


def test_portfolio_stats_aggregation():
    from debouw.ui.components import _portfolio_stats

    projects = [{"external_id": "a"}, {"external_id": "b"}, {"external_id": "c"}]
    assessments = {
        "a": {"overall_score": 0.7, "expected_delay_days": 100.0},
        "b": {"overall_score": 0.1, "expected_delay_days": 50.0},
    }
    s = _portfolio_stats(projects, assessments)
    assert s["n_projects"] == 3
    assert s["n_scored"] == 2
    assert s["n_elevated"] == 1  # only 0.7 >= 0.3
    assert abs(s["avg_score"] - 0.4) < 1e-9
    assert abs(s["avg_delay"] - 75.0) < 1e-9


def test_portfolio_stats_empty():
    from debouw.ui.components import _portfolio_stats

    s = _portfolio_stats([], {})
    assert s["n_projects"] == 0
    assert s["avg_score"] is None
    assert s["avg_delay"] is None
