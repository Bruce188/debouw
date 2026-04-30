"""
Integration sentinel for feat/brussels-score-differentiation.

This test is the integration proxy for the quantitative success criterion:
  - ≥ 3 distinct rounded overall_score values across 5 synthetic Brussels projects
  - ≥ 3 distinct top_categories[0].category_id values (regression guard against
    single-rule pinning)

The live-corpus gate (≥4 distinct rounded scores across the 50-dossier Brussels
corpus) is verified manually via:
  debouw reparse-brussels && debouw classify --reclassify-all
  sqlite3 data/debouw.db \
    "SELECT COUNT(DISTINCT ROUND(overall_score,2)) FROM risk_assessments
     WHERE engine_version='0.6.0'"

This pytest gate is the automation proxy, exercising the same code paths.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from debouw.config import Settings
from debouw.models.permit import (
    Address,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
    RiskCategory,
)
from debouw.risk.engine import RealRiskEngine
from debouw.risk.narrate import Narrator, ProjectNarration, RiskNarration

_T = datetime(2026, 4, 30, tzinfo=timezone.utc)


def _narrator_mock() -> Narrator:
    """Narrator mock that returns canned rationale — no network calls."""
    per_risk = {
        cat.value: RiskNarration(
            rationale_nl=f"Testoverzicht {cat.value}.",
            citations=[],
            certainty="laag",
        )
        for cat in RiskCategory
    }
    canned = ProjectNarration(summary_nl="Brussel testoverzicht.", per_risk=per_risk)
    mock_narrator = MagicMock(spec=Narrator)
    mock_narrator.narrate = AsyncMock(return_value=canned)
    return mock_narrator


def _bru_project(
    eid: str,
    *,
    floor_area_m2: float | None = None,
    error_weight: float | None = None,
    mer_status=None,
    description: str | None = None,
    trees_to_fell: int | None = None,
) -> PermitProject:
    return PermitProject(
        external_id=f"bru_sentinel:{eid}",
        source="brussels_openpermits",
        region="brussels",
        omv_reference=f"01/PU/{eid:0>7}",
        detail_url=f"https://openpermits.brussels/fr/_01/PU/{eid:0>7}",
        title=f"Brussels sentinel project {eid}",
        description=description,
        applicant_name=None,
        address=Address(
            raw="Rue du Midi 12 1000 Bruxelles",
            street="Rue du Midi 12",
            postcode="1000",
            municipality="Bruxelles",
            point=GeoPoint(lat=50.845, lon=4.353),
        ),
        project_type="PU",
        floors=None,
        height_m=None,
        units=None,
        parking_spaces=None,
        trees_to_fell=trees_to_fell,
        mer_status=mer_status,
        iioa_class=None,
        floor_area_m2=floor_area_m2,
        error_weight=error_weight,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=None,
        raw_html_path=Path("/tmp/bru_sentinel.html"),
        first_seen_at=_T,
        last_changed_at=_T,
        content_hash="b" * 64,
        decision_regime="post_2026_reform",
    )


@pytest.fixture
def bru_projects(monkeypatch) -> list[PermitProject]:
    """
    5 synthetic Brussels projects spanning distinct feature combinations so the
    engine produces ≥3 distinct rule top-categories and ≥3 distinct rounded scores.

    Each project is designed to have a clear primary signal:
      0: baseline (floor score, binding_advice_ignored leads)
      1: trees_to_fell → TREES_KAPVERG leads
      2: floors + motivated project → MOTIVATION_DEFECT or GRO_HEIGHT leads
      3: floor_area_m2=4000 (IIOA heuristic) + mer_status=screening → MER_SCREENING leads
      4: large area + FR ongunstig (binding advice) + mer_plicht → distinct high score

    ENABLE_IIOA_HEURISTIC=1 ensures heuristics can fire for large-area projects.
    """
    monkeypatch.setenv("ENABLE_IIOA_HEURISTIC", "1")
    return [
        # Project 0: empty / minimal Brussels project → baseline floor score
        _bru_project(
            "0001",
            floor_area_m2=None,
            error_weight=None,
            mer_status=None,
            description=None,
        ),
        # Project 1: trees → TREES_KAPVERG top rule
        _bru_project(
            "0002",
            floor_area_m2=None,
            error_weight=None,
            mer_status=None,
            description="Abattage de 12 arbres dans le parc de la ville",
            trees_to_fell=12,
        ),
        # Project 2: medium area → IIOA heuristic fires (≥1500 m²) + MER screening
        _bru_project(
            "0003",
            floor_area_m2=4000.0,
            error_weight=5.0,
            mer_status="screening",
            description="Immeuble de 30 appartements classé en zone industrielle",
        ),
        # Project 3: large area + MER heuristic fires (≥5000 m²) + FR ongunstig
        _bru_project(
            "0004",
            floor_area_m2=6000.0,
            error_weight=25.0,
            mer_status=None,
            description="avis défavorable de Bruxelles Environnement sur ce grand projet",
        ),
        # Project 4: large floor area + explicit mer_plicht + heritage binding advice
        _bru_project(
            "0005",
            floor_area_m2=12000.0,
            error_weight=100.0,
            mer_status="mer_plicht",
            description="Commission Royale des Monuments: avis défavorable. Très grand immeuble.",
        ),
    ]


def test_brussels_score_differentiation(bru_projects, monkeypatch):
    """
    Integration sentinel: 5 synthetic Brussels projects must produce ≥3 distinct
    rounded overall_score values, and ≥3 distinct top_categories[0] category IDs.

    This is the pytest proxy for the live-corpus SQL gate:
      SELECT COUNT(DISTINCT ROUND(overall_score,2)) FROM risk_assessments
      WHERE engine_version = '0.6.0'  → must be ≥4 on the live corpus.
    """
    s = Settings(anthropic_api_key=None, openai_api_key=None)
    engine = RealRiskEngine(s, narrator=_narrator_mock())

    assessments = [asyncio.run(engine.classify(p)) for p in bru_projects]

    # Primary gate: ≥3 distinct rounded overall_score values
    distinct_scores = {round(a.overall_score, 2) for a in assessments}
    assert len(distinct_scores) >= 3, (
        f"Expected ≥3 distinct rounded scores, got {len(distinct_scores)}: "
        f"{sorted(distinct_scores)}\n"
        f"Per-project scores: {[round(a.overall_score, 2) for a in assessments]}"
    )

    # Regression guard: ≥3 distinct top-category IDs (no single-rule pinning)
    top_cats = [
        assessments[i].top_risks[0].category.value
        for i in range(len(assessments))
        if assessments[i].top_risks
    ]
    distinct_top_cats = set(top_cats)
    assert len(distinct_top_cats) >= 3, (
        f"Expected ≥3 distinct top categories, got {len(distinct_top_cats)}: "
        f"{sorted(distinct_top_cats)}"
    )
