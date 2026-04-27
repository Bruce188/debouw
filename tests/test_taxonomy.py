"""Tests for risk/taxonomy.py — 14 entries + shape invariants."""

import pytest

from debouw.models.permit import RiskCategory
from debouw.risk.features import FeatureSet
from debouw.risk.taxonomy import TAXONOMY, _build_taxonomy_markdown


def test_all_14_categories_present():
    assert set(TAXONOMY.keys()) == set(RiskCategory)


def test_beta_keys_subset_of_featureset_fields():
    featureset_fields = set(FeatureSet.model_fields.keys())
    for cat, defn in TAXONOMY.items():
        bad = set(defn.beta_weights.keys()) - featureset_fields
        assert not bad, (
            f"Category {cat.value} has beta_weights referencing unknown FeatureSet fields: {bad}"
        )


def test_static_rationales_nonempty_dutch():
    for cat, defn in TAXONOMY.items():
        r = defn.static_rationale_nl
        assert len(r) >= 30, f"{cat.value}: static_rationale_nl too short ({len(r)} chars)"
        # Dutch text should contain common Dutch words
        has_dutch = any(
            word in r.lower()
            for word in ["de ", "het ", "een ", "van ", "om ", "te ", "op ", "in ", "is "]
        )
        assert has_dutch, f"{cat.value}: static_rationale_nl does not look like Dutch"


def test_severity_priors_in_range():
    for cat, defn in TAXONOMY.items():
        assert 30 <= defn.severity_prior_days <= 365, (
            f"{cat.value}: severity_prior_days={defn.severity_prior_days} outside [30, 365]"
        )


def test_base_success_rates_in_range():
    for cat, defn in TAXONOMY.items():
        assert 0.05 <= defn.base_success_rate <= 0.95, (
            f"{cat.value}: base_success_rate={defn.base_success_rate} outside [0.05, 0.95]"
        )


def test_evidence_keys_are_strings():
    for cat, defn in TAXONOMY.items():
        assert isinstance(defn.evidence_keys, tuple), f"{cat.value}: evidence_keys not tuple"
        for k in defn.evidence_keys:
            assert isinstance(k, str), f"{cat.value}: evidence_key {k!r} not str"


def test_label_nl_nonempty():
    for cat, defn in TAXONOMY.items():
        assert defn.label_nl, f"{cat.value}: label_nl is empty"


def test_legal_basis_nl_nonempty():
    for cat, defn in TAXONOMY.items():
        assert defn.legal_basis_nl, f"{cat.value}: legal_basis_nl is empty"


def test_typical_objector_template_nonempty():
    for cat, defn in TAXONOMY.items():
        assert defn.typical_objector_template_nl, f"{cat.value}: typical_objector_template_nl empty"


def test_project_modifier_callable():
    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS
    for cat, defn in TAXONOMY.items():
        proj = SYNTHETIC_PROJECTS[cat][0]
        result = defn.project_modifier(proj, proj.overlays)
        assert 0.7 <= result <= 1.4, (
            f"{cat.value}: project_modifier returned {result} outside [0.7, 1.4]"
        )


# ---------------------------------------------------------------------------
# Phase 5: Regional flag contract tests
# ---------------------------------------------------------------------------


def test_every_category_has_vl_in_applicable_regions():
    """Vlaanderen regression canary — no category may drop 'vl' from applicable_regions."""
    for cat, defn in TAXONOMY.items():
        assert "vl" in defn.applicable_regions, (
            f"Category {cat.value} is missing 'vl' in applicable_regions. "
            "This would break Vlaanderen processing and requires an engine_version bump."
        )


def test_vl_only_sentinels_are_correctly_flagged():
    """BPA_RUP_CONFLICT and WATER_FLOOD must be VL-only (VCRO/VMM overlay dependent)."""
    from debouw.risk.taxonomy import BPA_RUP_CONFLICT_DEF, WATER_FLOOD_DEF

    assert BPA_RUP_CONFLICT_DEF.applicable_regions == frozenset({"vl"}), (
        f"BPA_RUP_CONFLICT must be VL-only, got {BPA_RUP_CONFLICT_DEF.applicable_regions}"
    )
    assert WATER_FLOOD_DEF.applicable_regions == frozenset({"vl"}), (
        f"WATER_FLOOD must be VL-only, got {WATER_FLOOD_DEF.applicable_regions}"
    )
    # They must NOT include brussels
    assert "brussels" not in BPA_RUP_CONFLICT_DEF.applicable_regions
    assert "brussels" not in WATER_FLOOD_DEF.applicable_regions


def test_brussels_categories_have_fr_sibling_fields():
    """Every category with 'brussels' in applicable_regions must have all 4 FR fields."""
    for cat, defn in TAXONOMY.items():
        if "brussels" not in defn.applicable_regions:
            continue
        assert defn.label_fr is not None and len(defn.label_fr) > 0, (
            f"Category {cat.value}: label_fr is None or empty"
        )
        assert defn.legal_basis_fr is not None and len(defn.legal_basis_fr) > 0, (
            f"Category {cat.value}: legal_basis_fr is None or empty"
        )
        assert defn.static_rationale_fr is not None and len(defn.static_rationale_fr) > 0, (
            f"Category {cat.value}: static_rationale_fr is None or empty"
        )
        assert defn.typical_objector_template_fr is not None and len(
            defn.typical_objector_template_fr
        ) > 0, (
            f"Category {cat.value}: typical_objector_template_fr is None or empty"
        )


def test_french_taxonomy_markdown_excludes_vl_only_categories():
    """_build_taxonomy_markdown(fr, brussels) must exclude BPA_RUP_CONFLICT and WATER_FLOOD."""
    md = _build_taxonomy_markdown(language="fr", region="brussels")

    # Must NOT include VL-only category identifiers
    assert "bpa_rup_conflict" not in md, (
        "FR taxonomy markdown must not include bpa_rup_conflict (VL-only)"
    )
    assert "water_flood" not in md, (
        "FR taxonomy markdown must not include water_flood (VL-only)"
    )

    # Must include at least one French legal-basis token (CoBAT or art.)
    assert "CoBAT" in md or "art." in md, (
        "FR taxonomy markdown must include a French legal-basis reference (CoBAT or art.)"
    )

    # Must have exactly 12 categories (14 total - 2 VL-only)
    import re
    headings = re.findall(r"^### ", md, re.MULTILINE)
    assert len(headings) == 12, (
        f"FR taxonomy markdown must have 12 category headings, got {len(headings)}"
    )
