"""Tests for risk/rules.py — 14 fire/no-fire pairs."""

import pytest

from debouw.models.permit import RiskCategory
from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT, SYNTHETIC_PROJECTS
from debouw.risk.features import extract
from debouw.risk.rules import apply_all


def _hits_for(project, parcel_repeat_count: int = 0) -> dict:
    """Return a mapping of RiskCategory → RiskHit for a project."""
    features = extract(project, project.overlays, parcel_repeat_count=parcel_repeat_count)
    hits = apply_all(features, project.overlays, project)
    return {h.category: h for h in hits}


# ---------------------------------------------------------------------------
# apply_all structural invariants
# ---------------------------------------------------------------------------

def test_apply_all_returns_14_hits():
    features = extract(NEUTRAL_PROJECT, NEUTRAL_PROJECT.overlays)
    hits = apply_all(features, NEUTRAL_PROJECT.overlays, NEUTRAL_PROJECT)
    assert len(hits) == 14


def test_apply_all_unique_categories():
    features = extract(NEUTRAL_PROJECT, NEUTRAL_PROJECT.overlays)
    hits = apply_all(features, NEUTRAL_PROJECT.overlays, NEUTRAL_PROJECT)
    cats = [h.category for h in hits]
    assert len(cats) == len(set(cats))


def test_apply_all_deterministic_order():
    """Two calls produce identical category order."""
    features = extract(NEUTRAL_PROJECT, NEUTRAL_PROJECT.overlays)
    hits1 = apply_all(features, NEUTRAL_PROJECT.overlays, NEUTRAL_PROJECT)
    hits2 = apply_all(features, NEUTRAL_PROJECT.overlays, NEUTRAL_PROJECT)
    assert [h.category for h in hits1] == [h.category for h in hits2]


def test_apply_all_sorted_by_category_value():
    """Categories are in sorted(RiskCategory, key=value) order."""
    features = extract(NEUTRAL_PROJECT, NEUTRAL_PROJECT.overlays)
    hits = apply_all(features, NEUTRAL_PROJECT.overlays, NEUTRAL_PROJECT)
    values = [h.category.value for h in hits]
    assert values == sorted(values)


# ---------------------------------------------------------------------------
# GRO_HEIGHT
# ---------------------------------------------------------------------------

def test_gro_height_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0])
    h = hits[RiskCategory.GRO_HEIGHT]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_gro_height_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.GRO_HEIGHT].fired is False


# ---------------------------------------------------------------------------
# WATER_FLOOD
# ---------------------------------------------------------------------------

def test_water_flood_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.WATER_FLOOD][0])
    h = hits[RiskCategory.WATER_FLOOD]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_water_flood_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.WATER_FLOOD].fired is False


# ---------------------------------------------------------------------------
# MER_SCREENING
# ---------------------------------------------------------------------------

def test_mer_screening_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.MER_SCREENING][0])
    h = hits[RiskCategory.MER_SCREENING]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_mer_screening_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.MER_SCREENING].fired is False


# ---------------------------------------------------------------------------
# BPA_RUP_CONFLICT
# ---------------------------------------------------------------------------

def test_bpa_rup_conflict_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.BPA_RUP_CONFLICT][0])
    h = hits[RiskCategory.BPA_RUP_CONFLICT]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_bpa_rup_conflict_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.BPA_RUP_CONFLICT].fired is False


# ---------------------------------------------------------------------------
# MOTIVATION_DEFECT
# ---------------------------------------------------------------------------

def test_motivation_defect_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.MOTIVATION_DEFECT][0])
    h = hits[RiskCategory.MOTIVATION_DEFECT]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_motivation_defect_does_not_fire_on_neutral():
    """MOTIVATION_DEFECT should NOT fire on NEUTRAL_PROJECT (all None)."""
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.MOTIVATION_DEFECT].fired is False


# ---------------------------------------------------------------------------
# TREES_KAPVERG
# ---------------------------------------------------------------------------

def test_trees_kapverg_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.TREES_KAPVERG][0])
    h = hits[RiskCategory.TREES_KAPVERG]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_trees_kapverg_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.TREES_KAPVERG].fired is False


# ---------------------------------------------------------------------------
# MOBILITY_PARKING
# ---------------------------------------------------------------------------

def test_mobility_parking_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.MOBILITY_PARKING][0])
    h = hits[RiskCategory.MOBILITY_PARKING]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_mobility_parking_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.MOBILITY_PARKING].fired is False


# ---------------------------------------------------------------------------
# NATURE_2000_N
# ---------------------------------------------------------------------------

def test_nature_2000_n_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.NATURE_2000_N][0])
    h = hits[RiskCategory.NATURE_2000_N]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_nature_2000_n_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.NATURE_2000_N].fired is False


# ---------------------------------------------------------------------------
# HERITAGE_INV
# ---------------------------------------------------------------------------

def test_heritage_inv_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.HERITAGE_INV][0])
    h = hits[RiskCategory.HERITAGE_INV]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_heritage_inv_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.HERITAGE_INV].fired is False


# ---------------------------------------------------------------------------
# NUISANCE_NOISE
# ---------------------------------------------------------------------------

def test_nuisance_noise_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.NUISANCE_NOISE][0])
    h = hits[RiskCategory.NUISANCE_NOISE]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_nuisance_noise_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.NUISANCE_NOISE].fired is False


# ---------------------------------------------------------------------------
# PRIVACY_BEZONNING
# ---------------------------------------------------------------------------

def test_privacy_bezonning_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.PRIVACY_BEZONNING][0])
    h = hits[RiskCategory.PRIVACY_BEZONNING]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_privacy_bezonning_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.PRIVACY_BEZONNING].fired is False


# ---------------------------------------------------------------------------
# BINDING_ADVICE_IGNORED
# ---------------------------------------------------------------------------

def test_binding_advice_ignored_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.BINDING_ADVICE_IGNORED][0])
    h = hits[RiskCategory.BINDING_ADVICE_IGNORED]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_binding_advice_ignored_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.BINDING_ADVICE_IGNORED].fired is False


# ---------------------------------------------------------------------------
# FUNCTION_MIX_ZONING
# ---------------------------------------------------------------------------

def test_function_mix_zoning_fires_on_synthetic():
    hits = _hits_for(SYNTHETIC_PROJECTS[RiskCategory.FUNCTION_MIX_ZONING][0])
    h = hits[RiskCategory.FUNCTION_MIX_ZONING]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_function_mix_zoning_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.FUNCTION_MIX_ZONING].fired is False


# ---------------------------------------------------------------------------
# VERGUNNINGENCARROUSEL
# ---------------------------------------------------------------------------

def test_vergunningencarrousel_fires_on_synthetic():
    """Fires when parcel_repeat_count >= 2 (passed explicitly)."""
    project = SYNTHETIC_PROJECTS[RiskCategory.VERGUNNINGENCARROUSEL][0]
    hits = _hits_for(project, parcel_repeat_count=3)
    h = hits[RiskCategory.VERGUNNINGENCARROUSEL]
    assert h.fired is True
    assert len(h.evidence) >= 1


def test_vergunningencarrousel_does_not_fire_on_neutral():
    hits = _hits_for(NEUTRAL_PROJECT)
    assert hits[RiskCategory.VERGUNNINGENCARROUSEL].fired is False


def test_vergunningencarrousel_does_not_fire_with_count_one():
    """parcel_repeat_count=1 → does not trigger (threshold is 2)."""
    project = SYNTHETIC_PROJECTS[RiskCategory.VERGUNNINGENCARROUSEL][0]
    hits = _hits_for(project, parcel_repeat_count=1)
    assert hits[RiskCategory.VERGUNNINGENCARROUSEL].fired is False
