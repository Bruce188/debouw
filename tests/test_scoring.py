"""Tests for risk/scoring.py — math invariants."""

import math

import pytest

from debouw.models.permit import RiskCategory
from debouw.risk.scoring import (
    ALPHA,
    ScoredFactor,
    _clip,
    _confidence,
    _severity,
    _sigmoid,
    aggregate,
    top_k,
)


# ---------------------------------------------------------------------------
# _sigmoid
# ---------------------------------------------------------------------------

def test_sigmoid_monotonic():
    assert _sigmoid(-2) < _sigmoid(0) < _sigmoid(2)


def test_sigmoid_at_zero():
    assert _sigmoid(0) == pytest.approx(0.5, abs=1e-9)


def test_sigmoid_clamped_high():
    assert _sigmoid(50) <= 1.0 + 1e-9


def test_sigmoid_clamped_low():
    assert _sigmoid(-50) >= 0.0 - 1e-9


def test_sigmoid_symmetry():
    assert _sigmoid(1.0) == pytest.approx(1.0 - _sigmoid(-1.0), abs=1e-9)


# ---------------------------------------------------------------------------
# _clip
# ---------------------------------------------------------------------------

def test_clip_below():
    assert _clip(-0.5) == pytest.approx(0.0, abs=1e-9)


def test_clip_above():
    assert _clip(1.5) == pytest.approx(1.0, abs=1e-9)


def test_clip_within():
    assert _clip(0.5) == pytest.approx(0.5, abs=1e-9)


def test_clip_at_boundary():
    assert _clip(0.0) == pytest.approx(0.0, abs=1e-9)
    assert _clip(1.0) == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# _severity
# ---------------------------------------------------------------------------

def test_severity_in_unit_interval():
    """Any (prior_days, modifier) in [30..540] × [0.7..1.4] → severity in [0,1]."""
    for prior in [30, 90, 200, 360, 540]:
        for mod in [0.7, 1.0, 1.4]:
            s = _severity(prior, mod)
            assert 0.0 <= s <= 1.0, f"severity({prior}, {mod}) = {s} outside [0,1]"


def test_severity_monotonic_in_modifier():
    """Higher modifier → higher severity (same prior_days)."""
    assert _severity(200, 0.7) <= _severity(200, 1.4)


def test_severity_monotonic_in_days():
    """More delay days → higher severity."""
    assert _severity(30, 1.0) <= _severity(540, 1.0)


def test_severity_log1p_reference():
    """_severity(540, 1.0) should be 1.0 (reference maximum)."""
    s = _severity(540, 1.0)
    assert s == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# _confidence
# ---------------------------------------------------------------------------

def test_confidence_blend():
    """Known inputs → exact arithmetic."""
    c = _confidence(features_present=3, features_total=3, rule_specificity=1.0, precedent_support=0.0)
    expected = 0.4 * 1.0 + 0.3 * 0.0 + 0.3 * 1.0
    assert c == pytest.approx(expected, abs=1e-9)


def test_confidence_zero_total_features():
    """Zero features_total → completeness is 0."""
    c = _confidence(0, 0, 0.5, 0.0)
    assert c == pytest.approx(0.3 * 0.5, abs=1e-9)


def test_confidence_clipped_at_one():
    """Even with all perfect inputs, confidence is ≤ 1."""
    c = _confidence(10, 10, 1.0, 1.0)
    assert c <= 1.0 + 1e-9


def test_confidence_clipped_at_zero():
    """Confidence is always non-negative."""
    c = _confidence(0, 10, 0.0, 0.0)
    assert c >= 0.0


# ---------------------------------------------------------------------------
# ALPHA
# ---------------------------------------------------------------------------

def test_alpha_is_zero():
    """Phase 2: ALPHA = 0.0 (no precedent modifier)."""
    assert ALPHA == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# aggregate and top_k
# ---------------------------------------------------------------------------

def _make_factor(cat: RiskCategory, prob: float) -> ScoredFactor:
    return ScoredFactor(
        category=cat,
        probability=prob,
        severity=prob,
        expected_delay_days=100.0 * prob,
        confidence=0.5,
        evidence=[],
        typical_objector="test",
    )


def test_aggregate_max_score():
    cats = list(RiskCategory)
    factors = [_make_factor(c, i * 0.05) for i, c in enumerate(cats)]
    overall, _ = aggregate(factors)
    assert overall == pytest.approx(max(f.probability for f in factors), abs=1e-9)


def test_aggregate_delay_sum():
    cats = list(RiskCategory)[:3]
    factors = [_make_factor(c, 0.5) for c in cats]
    _, delay = aggregate(factors)
    expected = sum(0.5 * 50.0 for _ in cats)
    assert delay == pytest.approx(expected, abs=1e-9)


def test_aggregate_empty():
    overall, delay = aggregate([])
    assert overall == 0.0
    assert delay == 0.0


def test_top_k_tiebreak_on_category_value():
    """On equal probability, top_k orders by category.value asc."""
    cats = sorted(RiskCategory, key=lambda c: c.value)  # alphabetical
    # All same probability
    factors = [_make_factor(c, 0.5) for c in cats]
    result = top_k(factors, k=5)
    assert len(result) == 5
    # First should be smallest category value alphabetically
    result_cats = [f.category.value for f in result]
    assert result_cats == sorted(result_cats)


def test_top_k_sliced_to_k():
    cats = list(RiskCategory)
    factors = [_make_factor(c, 0.5) for c in cats]
    result = top_k(factors, k=3)
    assert len(result) == 3


def test_top_k_descending_by_probability():
    """Higher probability factors appear first."""
    factors = [
        _make_factor(RiskCategory.GRO_HEIGHT, 0.9),
        _make_factor(RiskCategory.WATER_FLOOD, 0.3),
        _make_factor(RiskCategory.MER_SCREENING, 0.7),
    ]
    result = top_k(factors, k=3)
    probs = [f.probability for f in result]
    assert probs == sorted(probs, reverse=True)


def test_top_k_respects_k_fewer_than_14():
    cats = list(RiskCategory)[:3]
    factors = [_make_factor(c, 0.5) for c in cats]
    result = top_k(factors, k=5)
    assert len(result) == 3  # fewer than k → return all


def test_scored_factor_is_frozen():
    """ScoredFactor is a frozen dataclass."""
    sf = _make_factor(RiskCategory.GRO_HEIGHT, 0.5)
    with pytest.raises(Exception):
        sf.probability = 0.9  # type: ignore
