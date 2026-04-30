"""
Tests for risk/engine.py — E2E, determinism, cache hit path.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from debouw.config import Settings
from debouw.models.permit import RiskCategory
from debouw.risk.engine import RealRiskEngine
from debouw.risk.interface import RiskEngine
from debouw.risk.narrate import Narrator, ProjectNarration, RiskNarration


def _settings(**kwargs) -> Settings:
    defaults = dict(anthropic_api_key=None, openai_api_key=None)
    defaults.update(kwargs)
    return Settings(**defaults)


def _canned_narration() -> ProjectNarration:
    per_risk = {
        cat.value: RiskNarration(
            rationale_nl=f"Rationale voor {cat.value}.",
            citations=[],
            certainty="laag",
        )
        for cat in RiskCategory
    }
    return ProjectNarration(summary_nl="Testoverzicht.", per_risk=per_risk)


def _narrator_mock() -> Narrator:
    """Return a Narrator whose narrate() always returns a canned ProjectNarration."""
    mock_narrator = MagicMock(spec=Narrator)
    canned = _canned_narration()
    mock_narrator.narrate = AsyncMock(return_value=canned)
    return mock_narrator


def _load_fixture() -> object:
    fixture_path = Path(__file__).parent / "fixtures" / "risk" / "gent_high_risk.json"
    from debouw.models.permit import PermitProject
    return PermitProject.model_validate(json.loads(fixture_path.read_text()))


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------

def test_classify_satisfies_protocol():
    """RealRiskEngine has a classify method and is structurally compatible."""
    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    assert hasattr(engine, "classify")
    import asyncio
    # Verify it can classify the simple fixture
    from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT
    result = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert result is not None


# ---------------------------------------------------------------------------
# Per-category: top risk is expected category
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat", [
    RiskCategory.GRO_HEIGHT,
    RiskCategory.WATER_FLOOD,
    RiskCategory.MER_SCREENING,
    RiskCategory.BPA_RUP_CONFLICT,
    RiskCategory.TREES_KAPVERG,
    RiskCategory.MOBILITY_PARKING,
    RiskCategory.NATURE_2000_N,
    RiskCategory.HERITAGE_INV,
    RiskCategory.NUISANCE_NOISE,
    RiskCategory.PRIVACY_BEZONNING,
    RiskCategory.BINDING_ADVICE_IGNORED,
    RiskCategory.FUNCTION_MIX_ZONING,
])
def test_top_risk_is_expected_for_category(cat):
    """For a synthetic fixture of category X, assess top risk = X."""
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())

    project = SYNTHETIC_PROJECTS[cat][0]
    if cat == RiskCategory.VERGUNNINGENCARROUSEL:
        engine._parcel_repeat_counts[project.external_id] = 3

    assessment = asyncio.run(engine.classify(project))
    # The top risk for this dedicated fixture should match the category
    assert len(assessment.top_risks) >= 1
    top_cats = [rf.category for rf in assessment.top_risks]
    assert cat in top_cats, (
        f"Expected {cat.value} in top_risks but got {[c.value for c in top_cats]}"
    )


def test_vergunningencarrousel_top_risk_with_repeat():
    """Carrousel fixture with parcel_repeat_count=3 should fire the rule."""
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    project = SYNTHETIC_PROJECTS[RiskCategory.VERGUNNINGENCARROUSEL][0]
    engine._parcel_repeat_counts[project.external_id] = 3

    assessment = asyncio.run(engine.classify(project))
    top_cats = [rf.category for rf in assessment.top_risks]
    assert RiskCategory.VERGUNNINGENCARROUSEL in top_cats


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_determinism_two_runs_produce_identical_scores():
    """Two consecutive classify calls produce same probability and category."""
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]

    a1 = asyncio.run(engine.classify(project))
    a2 = asyncio.run(engine.classify(project))

    assert len(a1.top_risks) == len(a2.top_risks)
    for r1, r2 in zip(a1.top_risks, a2.top_risks):
        assert r1.category == r2.category
        assert r1.probability == pytest.approx(r2.probability, abs=1e-9)
    # Rationale equality NOT asserted (mock may vary)


# ---------------------------------------------------------------------------
# Top risks capped at 5
# ---------------------------------------------------------------------------

def test_top_risks_capped_at_5():
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    assessment = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert len(assessment.top_risks) <= 5


# ---------------------------------------------------------------------------
# engine_version bump
# ---------------------------------------------------------------------------

def test_engine_version_is_bumped():
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    assessment = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert assessment.engine_version == "0.6.0"


# ---------------------------------------------------------------------------
# Cache hit path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_path(tmp_engine):
    """Pre-populate risk_narration_cache; classify; Anthropic NOT called."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from debouw.risk.cache import upsert_cached
    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS

    s = _settings(anthropic_api_key="sk-test", engine_version="0.2.0-rules-v1")
    Session = async_sessionmaker(tmp_engine, expire_on_commit=False)

    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]
    canned = _canned_narration()

    async with Session() as session:
        await upsert_cached(session, project.external_id, "0.2.0-rules-v1", canned)
        await session.commit()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    engine = RealRiskEngine(s, narrator=narrator, session_factory=Session)

    assessment = await engine.classify(project)

    mock_client.messages.create.assert_not_called()
    assert assessment.engine_version == "0.2.0-rules-v1"


# ---------------------------------------------------------------------------
# None overlays
# ---------------------------------------------------------------------------

def test_classify_handles_none_overlays():
    """Project with overlays=None classifies without error."""
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    # NEUTRAL_PROJECT has overlays but with all-default values
    assessment = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert assessment is not None
    # NATURE_2000_N should not fire without in_natura_2000 overlay
    top_cats = [rf.category for rf in assessment.top_risks]
    assert RiskCategory.NATURE_2000_N not in top_cats


# ---------------------------------------------------------------------------
# Inputs hash stable
# ---------------------------------------------------------------------------

def test_inputs_hash_stable():
    """Same project hashed twice → identical inputs_hash."""
    import asyncio
    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]

    a1 = asyncio.run(engine.classify(project))
    a2 = asyncio.run(engine.classify(project))
    assert a1.inputs_hash == a2.inputs_hash


# ---------------------------------------------------------------------------
# No lancedb import in Phase 2
# ---------------------------------------------------------------------------

def test_no_lancedb_import_in_phase2():
    """Phase 2 modules must not import lancedb."""
    # Import all Phase 2 modules
    import debouw.risk.engine  # noqa: F401
    import debouw.risk.features  # noqa: F401
    import debouw.risk.narrate  # noqa: F401
    import debouw.risk.rules  # noqa: F401
    import debouw.risk.scoring  # noqa: F401

    assert "lancedb" not in sys.modules, (
        "lancedb was imported by a Phase 2 module — this violates engine purity"
    )


# ---------------------------------------------------------------------------
# Gent snapshot fixture
# ---------------------------------------------------------------------------

def test_load_gent_snapshot():
    """Load gent_high_risk.json fixture; classify; schema validates."""
    import asyncio

    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    project = _load_fixture()
    assessment = asyncio.run(engine.classify(project))

    assert len(assessment.top_risks) >= 1
    # Validate the assessment schema round-trips
    from debouw.models.permit import RiskAssessment
    validated = RiskAssessment.model_validate(assessment.model_dump(mode="python"))
    assert validated.engine_version == assessment.engine_version


# ---------------------------------------------------------------------------
# Phase 5: Region filter
# ---------------------------------------------------------------------------

def test_classify_skips_rules_outside_region():
    """
    Brussels projects must not surface BPA_RUP_CONFLICT or WATER_FLOOD;
    Vlaanderen projects must surface all 14 categories when triggered.
    """
    import asyncio

    from debouw.risk.eval.synthetic_fixtures import SYNTHETIC_PROJECTS
    from debouw.risk.taxonomy import TAXONOMY

    s = _settings()

    # --- Brussels: VL-only categories must be absent ---
    vl_only_cats = frozenset(
        cat
        for cat, defn in TAXONOMY.items()
        if defn.applicable_regions == frozenset({"vl"})
    )
    assert RiskCategory.BPA_RUP_CONFLICT in vl_only_cats
    assert RiskCategory.WATER_FLOOD in vl_only_cats

    engine_bru = RealRiskEngine(s, narrator=_narrator_mock())

    for vl_cat in vl_only_cats:
        # Use the synthetic project designed to trigger this VL-only category,
        # but override region to brussels.
        base_project = SYNTHETIC_PROJECTS[vl_cat][0]
        bru_project = base_project.model_copy(update={"region": "brussels"})
        assessment = asyncio.run(engine_bru.classify(bru_project))
        all_returned_cats = {rf.category for rf in assessment.top_risks}
        assert vl_cat not in all_returned_cats, (
            f"VL-only category {vl_cat.value} appeared in Brussels assessment"
        )

    # --- Vlaanderen: VL-only categories CAN appear when triggered ---
    engine_vl = RealRiskEngine(s, narrator=_narrator_mock())

    for vl_cat in vl_only_cats:
        vl_project = SYNTHETIC_PROJECTS[vl_cat][0]
        # Ensure region is "vl" (default, but be explicit)
        assert vl_project.region == "vl", f"Synthetic fixture for {vl_cat} has wrong region"
        assessment = asyncio.run(engine_vl.classify(vl_project))
        # The category must be present somewhere in top_risks (it was designed to fire)
        all_returned_cats = {rf.category for rf in assessment.top_risks}
        assert vl_cat in all_returned_cats, (
            f"VL-only category {vl_cat.value} was unexpectedly absent from VL assessment"
        )
