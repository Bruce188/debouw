"""
Tests for the Phase 3 precedent_modifier wiring (α=0.4).

The Phase 2 contract that empty precedent_hits → modifier=1.0 → scoring
byte-identical is preserved. New tests cover the modifier formula
(weighted similarity × outcome weight) and its [0.6, 1.4] clamp.
"""

from __future__ import annotations

import random

import pytest

from debouw.models.permit import GeoOverlays, RiskCategory
from debouw.risk.precedents import PrecedentHit
from debouw.risk.rules import RiskHit
from debouw.risk.scoring import (
    ALPHA,
    precedent_modifier,
    score_hit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hit(similarity: float, outcome: str) -> PrecedentHit:
    from datetime import date
    return PrecedentHit(
        arrest_id=f"RVVB.A.2425.{random.randint(0, 9999):04d}",
        similarity=similarity,
        outcome=outcome,
        decision_excerpt="Test excerpt",
        grounds_used=["water_flood"],
        decision_date=date(2025, 1, 1),
    )


def _make_features():
    from debouw.risk.features import FeatureSet
    return FeatureSet()


def _make_project():
    """Reuse the synthetic NEUTRAL_PROJECT fixture for scoring math tests."""
    from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT
    return NEUTRAL_PROJECT


# ---------------------------------------------------------------------------
# ALPHA constant
# ---------------------------------------------------------------------------

def test_alpha_constant_is_0_4():
    assert ALPHA == pytest.approx(0.4, abs=1e-9)


# ---------------------------------------------------------------------------
# Modifier formula edge cases
# ---------------------------------------------------------------------------

def test_modifier_one_when_empty():
    assert precedent_modifier([]) == pytest.approx(1.0, abs=1e-9)


def test_modifier_max_when_all_vernietigd_high_sim():
    """All similarity=1.0 vernietigd hits → modifier = 1.0 + 0.4 × 1.0 = 1.4."""
    hits = [_make_hit(1.0, "vernietigd") for _ in range(3)]
    assert precedent_modifier(hits) == pytest.approx(1.4, abs=1e-9)


def test_modifier_min_when_all_verworpen_high_sim():
    """All similarity=1.0 verworpen hits → modifier = 1.0 + 0.4 × (-1.0) = 0.6."""
    hits = [_make_hit(1.0, "verworpen") for _ in range(3)]
    assert precedent_modifier(hits) == pytest.approx(0.6, abs=1e-9)


def test_modifier_neutral_when_all_zero_weight_outcomes():
    """outcomes with weight 0 → weighted_score=0 → modifier=1.0."""
    hits = [_make_hit(0.9, "andere") for _ in range(3)]
    assert precedent_modifier(hits) == pytest.approx(1.0, abs=1e-9)


def test_modifier_partial_with_gedeeltelijk():
    """All similarity=1.0 gedeeltelijk hits normalize to weighted_score=+1.0 → modifier=1.4.

    The formula normalizes by sum(|weight| × sim), so a uniform-weight cohort
    of any non-zero outcome saturates the direction signal. The mixed-outcome
    test below covers the cancelling case.
    """
    hits = [_make_hit(1.0, "gedeeltelijk") for _ in range(3)]
    assert precedent_modifier(hits) == pytest.approx(1.4, abs=1e-9)


def test_modifier_mixed_outcomes():
    """One vernietigd + one verworpen at sim=1.0 → cancels → 1.0."""
    hits = [_make_hit(1.0, "vernietigd"), _make_hit(1.0, "verworpen")]
    assert precedent_modifier(hits) == pytest.approx(1.0, abs=1e-9)


def test_modifier_invariant_in_random_hits():
    """100 random hit lists — all modifiers in [0.6, 1.4]."""
    random.seed(42)
    outcomes = ["vernietigd", "gedeeltelijk", "verworpen",
                "onontvankelijk", "afstand", "andere"]
    for _ in range(100):
        n = random.randint(1, 5)
        hits = [
            _make_hit(random.uniform(0.0, 1.0), random.choice(outcomes))
            for _ in range(n)
        ]
        m = precedent_modifier(hits)
        assert 0.6 - 1e-9 <= m <= 1.4 + 1e-9


# ---------------------------------------------------------------------------
# score_hit interplay
# ---------------------------------------------------------------------------

def test_score_hit_phase2_compat_no_precedents():
    """precedent_hits=None → modifier=1.0 → Phase 2 scoring (regression contract)."""
    from debouw.risk.taxonomy import TAXONOMY
    cat = RiskCategory.GRO_HEIGHT
    defn = TAXONOMY[cat]
    hit = RiskHit(category=cat, fired=True, evidence=["test"], trigger_features={})
    a = score_hit(hit, defn, _make_features(), _make_project(), GeoOverlays())
    b = score_hit(
        hit, defn, _make_features(), _make_project(), GeoOverlays(),
        precedent_hits=None,
    )
    c = score_hit(
        hit, defn, _make_features(), _make_project(), GeoOverlays(),
        precedent_hits=[],
    )
    assert a.probability == pytest.approx(b.probability, abs=1e-12)
    assert a.probability == pytest.approx(c.probability, abs=1e-12)


def test_score_hit_with_vernietigd_precedents_higher_probability():
    """Adding all-vernietigd precedents should not decrease probability."""
    from debouw.risk.taxonomy import TAXONOMY
    cat = RiskCategory.GRO_HEIGHT
    defn = TAXONOMY[cat]
    hit = RiskHit(category=cat, fired=True, evidence=["test"], trigger_features={})
    base = score_hit(hit, defn, _make_features(), _make_project(), GeoOverlays())
    with_hits = score_hit(
        hit, defn, _make_features(), _make_project(), GeoOverlays(),
        precedent_hits=[_make_hit(1.0, "vernietigd") for _ in range(3)],
    )
    assert with_hits.probability >= base.probability


def test_score_hit_with_verworpen_precedents_lower_probability():
    """Adding all-verworpen precedents should not increase probability."""
    from debouw.risk.taxonomy import TAXONOMY
    cat = RiskCategory.GRO_HEIGHT
    defn = TAXONOMY[cat]
    hit = RiskHit(category=cat, fired=True, evidence=["test"], trigger_features={})
    base = score_hit(hit, defn, _make_features(), _make_project(), GeoOverlays())
    with_hits = score_hit(
        hit, defn, _make_features(), _make_project(), GeoOverlays(),
        precedent_hits=[_make_hit(1.0, "verworpen") for _ in range(3)],
    )
    assert with_hits.probability <= base.probability
