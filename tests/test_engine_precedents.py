"""
Tests for engine ↔ precedents wiring.

Covers:
- engine_version is bumped to 0.3.0-rules-precedents-v1.
- Empty LanceDB store → score math reproduces Phase 2 (regression contract).
- precedent_hits are surfaced into RiskFactor.precedents in the assessment.
- engine.classify() does not call the network after init (post-init purity).
- Phase 2 test_engine.py behaviours still pass.

NO live LanceDB / OpenAI / Anthropic calls — embedder + LanceDB store are
either mocked or the engine's empty-vector graceful-degrade kicks in.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from debouw.config import Settings
from debouw.models.permit import RiskCategory
from debouw.risk.engine import RealRiskEngine
from debouw.risk.eval.synthetic_fixtures import NEUTRAL_PROJECT, SYNTHETIC_PROJECTS
from debouw.risk.narrate import Narrator, ProjectNarration, RiskNarration
from debouw.risk.precedents import PrecedentHit


def _settings(**kw) -> Settings:
    defaults = dict(anthropic_api_key=None, openai_api_key=None)
    defaults.update(kw)
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
    m = MagicMock(spec=Narrator)
    m.narrate = AsyncMock(return_value=_canned_narration())
    return m


# ---------------------------------------------------------------------------
# engine_version bump
# ---------------------------------------------------------------------------

def test_engine_version_bumped_to_0_3_0():
    """Engine version is now 0.6.0 (bumped in Phase 6 / feat/brussels-score-differentiation)."""
    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    assessment = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert assessment.engine_version == "0.6.0"


# ---------------------------------------------------------------------------
# Empty LanceDB → Phase 2 score parity
# ---------------------------------------------------------------------------

def test_empty_lancedb_yields_phase2_compatible_scores(tmp_path):
    """Empty LanceDB → modifier=1.0 → score math identical to Phase 2."""
    s1 = _settings(lancedb_path=tmp_path / "lance1")
    s2 = _settings(lancedb_path=tmp_path / "lance2")
    engine1 = RealRiskEngine(s1, narrator=_narrator_mock())
    engine2 = RealRiskEngine(s2, narrator=_narrator_mock())
    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]
    a1 = asyncio.run(engine1.classify(project))
    a2 = asyncio.run(engine2.classify(project))
    # Two fresh engines, both with empty LanceDB → byte-identical probabilities.
    assert len(a1.top_risks) == len(a2.top_risks)
    for r1, r2 in zip(a1.top_risks, a2.top_risks):
        assert r1.category == r2.category
        assert r1.probability == pytest.approx(r2.probability, abs=1e-9)
    # Phase 2 contract: precedents list empty when LanceDB has no rows.
    for rf in a1.top_risks:
        assert rf.precedents == []


# ---------------------------------------------------------------------------
# Precedent population path (bypass LanceDB native — inject hits directly)
# ---------------------------------------------------------------------------

def test_precedents_surfaced_when_store_returns_hits():
    """Patched store returns synthetic hits → engine wires them into RiskFactor."""
    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())

    # Patch the store: search returns synthetic hits for GRO_HEIGHT, [] otherwise.
    fake_hit = PrecedentHit(
        arrest_id="RVVB.A.2425.0312",
        similarity=0.92,
        outcome="vernietigd",
        decision_excerpt="Synthetic excerpt for the test.",
        grounds_used=["gro_height"],
        decision_date=date(2025, 1, 1),
    )

    def _fake_search(category, vec, **kw):
        return [fake_hit] if category == RiskCategory.GRO_HEIGHT else []

    engine._precedent_store.search = _fake_search  # type: ignore[assignment]
    # Pre-seed the engine query-vector cache so search() is reached even
    # without OpenAI (review-v5 B4 — warmup is hoisted out of classify; the
    # engine no longer falls back to the store's cache).
    for cat in RiskCategory:
        engine._query_vectors_by_category[cat] = [0.1] * s.embedding_dim
    engine._query_vectors_built = True

    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]
    assessment = asyncio.run(engine.classify(project))

    # The top GRO_HEIGHT factor should now carry one precedent.
    gro_factors = [
        rf for rf in assessment.top_risks if rf.category == RiskCategory.GRO_HEIGHT
    ]
    assert gro_factors, "GRO_HEIGHT must be in top_risks for this fixture"
    rf = gro_factors[0]
    assert len(rf.precedents) == 1
    assert rf.precedents[0].precedent_id == "RVVB.A.2425.0312"
    assert rf.precedents[0].outcome == "vernietigd"
    assert rf.precedents[0].similarity == pytest.approx(0.92, abs=1e-6)


def test_precedent_hits_count_capped_at_two():
    """Engine must cap precedents per-factor at <=2 (Phase 3 plan §3.3)."""
    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())
    hits = [
        PrecedentHit(
            arrest_id=f"RVVB.A.2425.{i:04d}",
            similarity=0.9 - i * 0.01,
            outcome="vernietigd",
            decision_excerpt=f"Excerpt {i}",
            grounds_used=["gro_height"],
            decision_date=date(2025, 1, 1),
        )
        for i in range(5)
    ]
    engine._precedent_store.search = lambda *a, **k: hits  # type: ignore[assignment]
    # review-v5 B4: seed engine cache (warmup hoisted out of classify).
    for cat in RiskCategory:
        engine._query_vectors_by_category[cat] = [0.1] * s.embedding_dim
    engine._query_vectors_built = True

    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]
    assessment = asyncio.run(engine.classify(project))
    for rf in assessment.top_risks:
        assert len(rf.precedents) <= 2, (
            f"{rf.category.value} has {len(rf.precedents)} precedents (>2 cap)"
        )


# ---------------------------------------------------------------------------
# Score modification when precedents present (vs empty store)
# ---------------------------------------------------------------------------

def test_seeded_precedents_modify_top_risk_probability():
    """All-vernietigd hits → modifier > 1.0 → probability >= empty-store baseline."""
    s = _settings()

    # Baseline (no precedents)
    engine_base = RealRiskEngine(s, narrator=_narrator_mock())
    project = SYNTHETIC_PROJECTS[RiskCategory.GRO_HEIGHT][0]
    a_base = asyncio.run(engine_base.classify(project))
    base_top = next(
        rf for rf in a_base.top_risks if rf.category == RiskCategory.GRO_HEIGHT
    )

    # With precedents (vernietigd → modifier 1.4)
    engine_seeded = RealRiskEngine(s, narrator=_narrator_mock())
    fake_hits = [
        PrecedentHit(
            arrest_id=f"RVVB.A.2425.{i:04d}",
            similarity=1.0,
            outcome="vernietigd",
            decision_excerpt="Hit",
            grounds_used=["gro_height"],
            decision_date=date(2025, 1, 1),
        )
        for i in range(3)
    ]
    engine_seeded._precedent_store.search = (  # type: ignore[assignment]
        lambda category, vec, **kw: fake_hits if category == RiskCategory.GRO_HEIGHT else []
    )
    # review-v5 B4: seed engine cache (warmup hoisted out of classify).
    for cat in RiskCategory:
        engine_seeded._query_vectors_by_category[cat] = [0.1] * s.embedding_dim
    engine_seeded._query_vectors_built = True
    a_seeded = asyncio.run(engine_seeded.classify(project))
    seeded_top = next(
        rf for rf in a_seeded.top_risks if rf.category == RiskCategory.GRO_HEIGHT
    )
    assert seeded_top.probability >= base_top.probability


# ---------------------------------------------------------------------------
# No-network post-init purity
# ---------------------------------------------------------------------------

def test_classify_no_anthropic_call_after_init(monkeypatch):
    """
    classify() must not call Anthropic OR OpenAI — the narrator is the only
    Anthropic surface (mocked here), and OpenAI embeddings are hoisted into
    ``warmup()`` (review-v5 B4). Both clients are armed to raise; classify
    must stay silent.
    """
    s = _settings()
    # Anthropic narrator client armed to raise.
    narrator = Narrator(s)
    boom = MagicMock()
    boom.messages.create = AsyncMock(
        side_effect=AssertionError("Anthropic called inside classify()")
    )
    narrator._anthropic_client = boom  # type: ignore[attr-defined]
    # Override narrate to swallow the call (the test is about the engine, not narrator).
    narrator.narrate = AsyncMock(return_value=_canned_narration())  # type: ignore[assignment]

    engine = RealRiskEngine(s, narrator=narrator)
    # Arm the OpenAI embedder client too — classify() must not touch it
    # (warmup is the only legitimate caller, and we never invoke it here).
    boom_openai = MagicMock()
    boom_openai.embeddings.create = AsyncMock(
        side_effect=AssertionError("OpenAI called inside classify()")
    )
    engine._precedent_store._embedder = boom_openai  # type: ignore[attr-defined]

    assessment = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert assessment is not None


def test_classify_does_not_embed_during_classify(monkeypatch):
    """
    classify() must not call ``embed_query_for_category`` — that method is
    OpenAI-bound and was hoisted into ``warmup()`` (review-v5 B4). Calling it
    inside classify would re-introduce a network round-trip per project.
    """
    s = _settings()
    engine = RealRiskEngine(s, narrator=_narrator_mock())

    embed_calls: list[RiskCategory] = []

    async def boom_embed(category):
        embed_calls.append(category)
        raise AssertionError(
            f"embed_query_for_category called inside classify() for {category}"
        )

    engine._precedent_store.embed_query_for_category = boom_embed  # type: ignore[assignment]

    # classify() must run cleanly — warmup() is the only legitimate caller.
    assessment = asyncio.run(engine.classify(NEUTRAL_PROJECT))
    assert assessment is not None
    assert embed_calls == [], (
        f"embed_query_for_category was called inside classify(): {embed_calls}"
    )


# ---------------------------------------------------------------------------
# CLI smoke: `debouw eval` echoes "insufficient_gold_set" on small N
# ---------------------------------------------------------------------------

def test_cli_eval_smoke_outputs_insufficient_gold_set(tmp_path, monkeypatch):
    """The CLI eval command runs end-to-end on the synthetic gold set."""
    from typer.testing import CliRunner

    from debouw.cli import app

    # Point the synthetic gold-set path at the fixture
    fixture = "tests/fixtures/rvvb/gold_set_synthetic.jsonl"

    # Use tmp_path for db so we don't hit a real one
    monkeypatch.setenv("DB_PATH", str(tmp_path / "smoke.sqlite"))
    runner = CliRunner()
    result = runner.invoke(app, ["eval", "--gold-set", fixture])
    assert result.exit_code == 0, result.stdout
    assert "insufficient_gold_set" in result.stdout
