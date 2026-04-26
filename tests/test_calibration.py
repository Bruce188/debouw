"""
Tests for risk/calibration.py — math + insufficient_gold_set gate.

The math primitives are tested in isolation (no DB). The end-to-end
``run_calibration`` flow is tested via the synthetic gold set fixture and
the public engine in ``test_engine_precedents.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from debouw.models.permit import RiskCategory
from debouw.risk.calibration import (
    CalibrationReport,
    GoldCase,
    _brier_for_case,
    _calibration_bins,
    _p_at_5_for_case,
    load_gold_set,
)


_SYN_GOLD = Path(__file__).parent / "fixtures" / "rvvb" / "gold_set_synthetic.jsonl"


# ---------------------------------------------------------------------------
# load_gold_set
# ---------------------------------------------------------------------------

def test_load_gold_set_reads_synthetic():
    cases = load_gold_set(_SYN_GOLD)
    assert len(cases) == 5
    assert cases[0].project_external_id == "synthetic_case_1"
    # GRO_HEIGHT is the first expected category in case 1
    assert RiskCategory.GRO_HEIGHT in cases[0].expected_top_categories


def test_load_gold_set_skips_blank_lines(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(
        '\n\n{"project_external_id": "a", '
        '"expected_top_categories": ["water_flood"]}\n\n',
        encoding="utf-8",
    )
    cases = load_gold_set(p)
    assert len(cases) == 1


def test_load_gold_set_drops_unknown_category(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(
        '{"project_external_id": "a", '
        '"expected_top_categories": ["water_flood", "foobar"]}\n',
        encoding="utf-8",
    )
    cases = load_gold_set(p)
    assert len(cases) == 1
    assert cases[0].expected_top_categories == [RiskCategory.WATER_FLOOD]


# ---------------------------------------------------------------------------
# P@5
# ---------------------------------------------------------------------------

def test_p_at_5_perfect_match():
    """All 2 expected categories in top_5 → P@5 = 1.0."""
    expected = [RiskCategory.GRO_HEIGHT, RiskCategory.WATER_FLOOD]
    top5 = [
        RiskCategory.GRO_HEIGHT,
        RiskCategory.WATER_FLOOD,
        RiskCategory.HERITAGE_INV,
        RiskCategory.MER_SCREENING,
        RiskCategory.MOBILITY_PARKING,
    ]
    assert _p_at_5_for_case(top5, expected) == pytest.approx(1.0, abs=1e-9)


def test_p_at_5_partial():
    """1 of 2 expected in top_5 → 0.5."""
    expected = [RiskCategory.GRO_HEIGHT, RiskCategory.WATER_FLOOD]
    top5 = [
        RiskCategory.GRO_HEIGHT,
        RiskCategory.HERITAGE_INV,
        RiskCategory.MER_SCREENING,
        RiskCategory.MOBILITY_PARKING,
        RiskCategory.NUISANCE_NOISE,
    ]
    assert _p_at_5_for_case(top5, expected) == pytest.approx(0.5, abs=1e-9)


def test_p_at_5_no_match():
    expected = [RiskCategory.GRO_HEIGHT]
    top5 = [
        RiskCategory.WATER_FLOOD,
        RiskCategory.HERITAGE_INV,
        RiskCategory.MER_SCREENING,
        RiskCategory.MOBILITY_PARKING,
        RiskCategory.NUISANCE_NOISE,
    ]
    assert _p_at_5_for_case(top5, expected) == pytest.approx(0.0, abs=1e-9)


def test_p_at_5_empty_expected_returns_zero():
    """No expected categories → trivially 0 (case skipped from contribution)."""
    assert _p_at_5_for_case([RiskCategory.GRO_HEIGHT], []) == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Brier
# ---------------------------------------------------------------------------

def test_brier_zero_for_perfect_predictions():
    """Predictions match observed exactly → contribution = 0."""
    predicted = {
        RiskCategory.GRO_HEIGHT: 1.0,
        RiskCategory.WATER_FLOOD: 0.0,
    }
    expected = [RiskCategory.GRO_HEIGHT]
    total, n = _brier_for_case(predicted, expected)
    assert total == pytest.approx(0.0, abs=1e-9)
    assert n == 2


def test_brier_arithmetic_known_inputs():
    """Predictions [0.6, 0.3] for [+1, 0] → SE = 0.16 + 0.09 = 0.25."""
    predicted = {
        RiskCategory.GRO_HEIGHT: 0.6,
        RiskCategory.WATER_FLOOD: 0.3,
    }
    expected = [RiskCategory.GRO_HEIGHT]
    total, n = _brier_for_case(predicted, expected)
    assert total == pytest.approx(0.16 + 0.09, abs=1e-9)
    assert n == 2


def test_brier_empty_predictions_zero():
    total, n = _brier_for_case({}, [RiskCategory.GRO_HEIGHT])
    assert total == 0.0
    assert n == 0


# ---------------------------------------------------------------------------
# Calibration bins
# ---------------------------------------------------------------------------

def test_calibration_bins_count():
    """Always emits exactly num_bins entries."""
    pairs = [(0.05, 0.0), (0.15, 1.0), (0.55, 1.0)]
    bins = _calibration_bins(pairs, num_bins=10)
    assert len(bins) == 10


def test_calibration_bins_groups_correctly():
    """Pairs at predicted=0.55 land in bin 5."""
    pairs = [(0.55, 1.0), (0.55, 0.0)]
    bins = _calibration_bins(pairs, num_bins=10)
    counts = [b[2] for b in bins]
    assert counts[5] == 2
    assert sum(counts) == 2


def test_calibration_bins_handles_predicted_one():
    """predicted=1.0 → last bin (avoids index out of range)."""
    pairs = [(1.0, 1.0)]
    bins = _calibration_bins(pairs, num_bins=10)
    counts = [b[2] for b in bins]
    assert counts[9] == 1


# ---------------------------------------------------------------------------
# GoldCase
# ---------------------------------------------------------------------------

def test_gold_case_from_json_roundtrip():
    raw = {
        "project_external_id": "x",
        "expected_top_categories": ["water_flood", "gro_height"],
        "actual_outcome": "vernietigd",
        "notes": "hello",
    }
    case = GoldCase.from_json(raw)
    assert case.project_external_id == "x"
    assert RiskCategory.WATER_FLOOD in case.expected_top_categories
    assert case.actual_outcome == "vernietigd"


# ---------------------------------------------------------------------------
# Insufficient gold set gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insufficient_gold_set_path(tmp_path, tmp_engine):
    """n=5 < gold_set_min_n=30 → metrics None, gates flagged insufficient."""
    from unittest.mock import AsyncMock, MagicMock

    from debouw.config import Settings
    from debouw.risk.calibration import run_calibration

    # Settings pointing at the tmp DB
    s = Settings(
        anthropic_api_key=None,
        openai_api_key=None,
        gold_set_min_n=30,
        db_path=tmp_engine.url.database,
    )

    # Patched engine so calibration loop runs without LanceDB or DB rows
    fake_engine = MagicMock()
    fake_engine.classify = AsyncMock(return_value=MagicMock(top_risks=[]))
    # Calibration calls warmup() before the per-case loop (review-v5 N1).
    fake_engine.warmup = AsyncMock(return_value=None)

    # Even though the project rows are missing, calibration should swallow that
    # and still return an "insufficient_gold_set" report (metrics None).
    report = await run_calibration(
        s, gold_set_path=_SYN_GOLD, engine=fake_engine,
    )
    assert isinstance(report, CalibrationReport)
    assert report.n == 5
    assert report.p_at_5 is None
    assert report.brier is None
    assert all(v == "insufficient_gold_set" for v in report.gates.values())
    assert len(report.calibration_bins) == 10


@pytest.mark.asyncio
async def test_calibration_report_is_frozen(tmp_path, tmp_engine):
    """Report is a frozen dataclass — fields cannot be mutated."""
    from dataclasses import FrozenInstanceError
    report = CalibrationReport(
        n=0,
        p_at_5=None,
        brier=None,
        calibration_bins=[],
        gates={},
    )
    with pytest.raises(FrozenInstanceError):
        report.n = 1  # type: ignore[misc]
