"""
Deterministic scoring math for the debouw risk engine.

All functions are pure (no network, no DB). Determinism is guaranteed by:
- sorted-key iteration in _trigger_prob
- float clamping to [0, 1]
- tiebreak on category.value (alphabetic) in top_k
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from debouw.models.permit import GeoOverlays, PermitProject, RiskCategory
from debouw.risk.features import FeatureSet
from debouw.risk.taxonomy import RiskCategoryDef

if TYPE_CHECKING:
    from debouw.risk.precedents import PrecedentHit

# Precedent modifier weight — Phase 3: α=0.4.
# Phase 2 regression: empty hits → precedent_modifier=1.0 → score byte-identical.
ALPHA: float = 0.4

# Outcome weights for precedent_modifier calculation
_OUTCOME_WEIGHT: dict[str, float] = {
    "vernietigd":     +1.0,
    "gedeeltelijk":   +0.5,
    "verworpen":      -1.0,
    "onontvankelijk":  0.0,
    "afstand":         0.0,
    "andere":          0.0,
}


# ---------------------------------------------------------------------------
# Math primitives
# ---------------------------------------------------------------------------

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _trigger_prob(beta: Mapping[str, float], features: FeatureSet) -> float:
    """
    Logistic trigger probability.

    Iterates beta keys in sorted order for determinism. Missing or None
    feature values contribute 0.
    """
    linear = 0.0
    for key in sorted(beta):
        raw = getattr(features, key, None)
        if raw is None:
            continue
        # bool → float
        linear += beta[key] * (1.0 if raw is True else (0.0 if raw is False else float(raw)))
    return _clip(_sigmoid(linear))


def _severity(severity_prior_days: int, project_modifier: float) -> float:
    """
    Rescale expected delay to [0, 1] using log1p(540) as the reference maximum.
    """
    return _clip(
        math.log1p(severity_prior_days * project_modifier) / math.log1p(540),
        0.0,
        1.0,
    )


def _confidence(
    features_present: int,
    features_total: int,
    rule_specificity: float,
    precedent_support: float = 0.0,
) -> float:
    """
    Blend of data completeness, precedent support, and rule specificity.

    0.4 × completeness + 0.3 × precedent_support + 0.3 × rule_specificity
    """
    completeness = features_present / features_total if features_total > 0 else 0.0
    return _clip(
        0.4 * completeness + 0.3 * precedent_support + 0.3 * rule_specificity
    )


# ---------------------------------------------------------------------------
# Precedent modifier
# ---------------------------------------------------------------------------

def precedent_modifier(hits: list["PrecedentHit"]) -> float:
    """
    Outcome-weighted similarity → modifier in [0.6, 1.4]. Empty hits → 1.0.

    Formula (plan-v4 § 3.1):
        weighted_score = Σ(similarity_i × weight_i) / Σ(|weight_i| × similarity_i)
        modifier = clip(1.0 + ALPHA × weighted_score, 0.6, 1.4)

    Phase 2 regression contract: empty hits → modifier = 1.0 exactly.
    """
    if not hits:
        return 1.0
    weighted = sum(
        h.similarity * _OUTCOME_WEIGHT.get(h.outcome, 0.0) for h in hits
    )
    norm = sum(
        abs(_OUTCOME_WEIGHT.get(h.outcome, 0.0)) * h.similarity for h in hits
    )
    if norm == 0.0:
        return 1.0
    score = weighted / norm  # ∈ [-1, +1]
    return _clip(1.0 + ALPHA * score, 0.6, 1.4)


# ---------------------------------------------------------------------------
# Scored factor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredFactor:
    """
    Fully-scored risk factor for one category.

    rationale is filled later by the narrator (engine.py); it defaults to
    empty string here so ScoredFactor is self-contained.
    """

    category: RiskCategory
    probability: float        # trigger × success
    severity: float           # rescaled to [0, 1]
    expected_delay_days: float
    confidence: float
    evidence: list[str]
    typical_objector: str
    rationale: str = ""


# ---------------------------------------------------------------------------
# Hit (from rules.py) → ScoredFactor
# ---------------------------------------------------------------------------

def score_hit(
    hit: "RiskHit",  # type: ignore[name-defined]  # forward ref from rules.py
    taxonomy_def: RiskCategoryDef,
    features: FeatureSet,
    project: PermitProject,
    overlays: GeoOverlays | None,
    *,
    precedent_hits: list["PrecedentHit"] | None = None,  # Phase 3: optional precedent wiring
) -> ScoredFactor:
    """
    Convert a rule hit to a fully-scored factor.

    trigger_prob drives the raw firing probability.
    success_prob = base_success_rate × precedent_modifier(hits).
    probability = trigger_prob × success_prob.

    Phase 2 regression contract: precedent_hits=None (or []) → precedent_modifier=1.0
    → scoring byte-identical to Phase 2.
    """
    modifier = taxonomy_def.project_modifier(project, overlays)
    trigger = _trigger_prob(taxonomy_def.beta_weights, features)
    hits = precedent_hits or []
    prec_mod = precedent_modifier(hits)
    success = taxonomy_def.base_success_rate * prec_mod

    # Gate: when the rule did not fire, attenuate probability by 0.2.
    # This ensures non-triggered categories remain low-probability even
    # when their beta weights pick up signal from unrelated features.
    if not hit.fired:
        trigger = trigger * 0.2

    probability = _clip(trigger * success)
    sev = _severity(taxonomy_def.severity_prior_days, modifier)
    expected_delay = taxonomy_def.severity_prior_days * modifier

    # Count evidence / features present for confidence
    beta_keys = list(taxonomy_def.beta_weights.keys())
    features_present = sum(
        1 for k in beta_keys
        if getattr(features, k, None) not in (None, False, 0, 0.0)
    )
    rule_specificity = len(beta_keys) / max(1, len(beta_keys) + 2)

    # precedent_support scales with hit count (max at 3+ hits)
    precedent_support = min(1.0, len(hits) / 3.0)
    conf = _confidence(features_present, len(beta_keys), rule_specificity, precedent_support)

    return ScoredFactor(
        category=hit.category,
        probability=probability,
        severity=sev,
        expected_delay_days=expected_delay,
        confidence=conf,
        evidence=sorted(hit.evidence),
        typical_objector=taxonomy_def.typical_objector_template_nl,
    )


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def aggregate(factors: list[ScoredFactor]) -> tuple[float, float]:
    """
    Return (overall_score, expected_delay_days) over all 14 factors.

    overall_score = max(probability).
    expected_delay_days = Σ probability_c × expected_delay_days_c.
    """
    if not factors:
        return 0.0, 0.0
    overall = max(f.probability for f in factors)
    delay = sum(f.probability * f.expected_delay_days for f in factors)
    return overall, delay


def top_k(factors: list[ScoredFactor], k: int = 5) -> list[ScoredFactor]:
    """
    Return top-k factors sorted by (probability desc, category.value asc).

    Tie-breaking on category.value ensures determinism.
    """
    sorted_factors = sorted(
        factors,
        key=lambda f: (-f.probability, f.category.value),
    )
    return sorted_factors[:k]


# ---------------------------------------------------------------------------
# Type alias for forward reference
# ---------------------------------------------------------------------------

class RiskHit:
    """Placeholder so scoring.py is importable without importing rules.py.

    The real RiskHit lives in rules.py. scoring.py only uses it as a type
    hint in score_hit() — no circular import occurs because rules.py imports
    from scoring.py (not vice versa).
    """

    category: RiskCategory
    fired: bool
    evidence: list[str]
    trigger_features: dict[str, float]
