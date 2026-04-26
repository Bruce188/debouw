"""Tests for risk/taxonomy.py — 14 entries + shape invariants."""

import pytest

from debouw.models.permit import RiskCategory
from debouw.risk.features import FeatureSet
from debouw.risk.taxonomy import TAXONOMY


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
