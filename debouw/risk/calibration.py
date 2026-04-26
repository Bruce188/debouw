"""
Backtest harness for the risk engine.

Reads a JSONL gold set (one GoldCase per line), classifies each project via
the engine, and reports P@5 + Brier + reliability bins.

Gate logic per analysis-v4 § 9 Q4-Q5: when the gold set has fewer rows than
``settings.gold_set_min_n`` (default 30), the metrics are emitted as ``None``
and the gates dict marks every gate ``"insufficient_gold_set"``. Three seed
cases ship in ``debouw/risk/eval/gold_set.jsonl``; the user is expected to
hand-label the remainder of the corpus post-merge.

NOT inside engine.classify(). The harness is a CLI-level driver that creates
its own engine + DB session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from debouw.models.permit import RiskCategory

if TYPE_CHECKING:
    from debouw.config import Settings
    from debouw.risk.engine import RealRiskEngine

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# GoldCase
# ---------------------------------------------------------------------------

OutcomeLiteral = Literal[
    "vernietigd", "gedeeltelijk", "verworpen", "onontvankelijk", "afstand", "andere"
]


@dataclass(frozen=True)
class GoldCase:
    """One labelled case from the gold set."""

    project_external_id: str
    expected_top_categories: list[RiskCategory]
    actual_outcome: OutcomeLiteral | None = None
    notes: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "GoldCase":
        cats: list[RiskCategory] = []
        for c in raw.get("expected_top_categories", []):
            try:
                cats.append(RiskCategory(c))
            except ValueError:
                log.warning("calibration_unknown_category", category=c)
        outcome = raw.get("actual_outcome")
        return cls(
            project_external_id=raw["project_external_id"],
            expected_top_categories=cats,
            actual_outcome=outcome,  # type: ignore[arg-type]
            notes=raw.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# CalibrationReport
# ---------------------------------------------------------------------------

GateStatus = Literal["pass", "fail", "insufficient_gold_set"]


@dataclass(frozen=True)
class CalibrationReport:
    """Aggregated metrics across the gold set."""

    n: int
    p_at_5: float | None
    brier: float | None
    calibration_bins: list[tuple[float, float, int]]
    gates: dict[str, GateStatus] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _p_at_5_for_case(top5: list[RiskCategory], expected: list[RiskCategory]) -> float:
    """Fraction of expected categories present in the top-5 prediction.

    When `expected` is empty, returns 0.0 (case skipped from contribution).
    """
    if not expected:
        return 0.0
    expected_set = set(expected)
    top5_set = set(top5[:5])
    hits = len(expected_set & top5_set)
    return hits / len(expected_set)


def _brier_for_case(
    predicted: dict[RiskCategory, float], expected: list[RiskCategory]
) -> tuple[float, int]:
    """Sum-of-squared-error contribution for one case + count of categories considered.

    Brier here is computed per (case, category) over all categories that have
    a predicted probability. The observed value is 1.0 when the category is in
    `expected_top_categories`, else 0.0.
    """
    if not predicted:
        return 0.0, 0
    expected_set = set(expected)
    total = 0.0
    n = 0
    for cat, prob in predicted.items():
        observed = 1.0 if cat in expected_set else 0.0
        total += (prob - observed) ** 2
        n += 1
    return total, n


def _calibration_bins(
    pairs: list[tuple[float, float]], num_bins: int = 10
) -> list[tuple[float, float, int]]:
    """Reliability-diagram bins. `pairs` = [(predicted, observed), ...].

    Returns one tuple per bin: (mean predicted, mean observed, count). Empty
    bins emit (centre, 0.0, 0).
    """
    width = 1.0 / num_bins
    bins: list[list[tuple[float, float]]] = [[] for _ in range(num_bins)]
    for p, o in pairs:
        idx = min(int(p / width), num_bins - 1)
        bins[idx].append((p, o))

    out: list[tuple[float, float, int]] = []
    for i, bucket in enumerate(bins):
        if not bucket:
            centre = (i + 0.5) * width
            out.append((centre, 0.0, 0))
            continue
        mean_p = sum(p for p, _ in bucket) / len(bucket)
        mean_o = sum(o for _, o in bucket) / len(bucket)
        out.append((mean_p, mean_o, len(bucket)))
    return out


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_gold_set(path: Path) -> list[GoldCase]:
    """Read a JSONL gold-set file. Skips blank lines."""
    cases: list[GoldCase] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            cases.append(GoldCase.from_json(raw))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning(
                "calibration_gold_set_line_skipped",
                path=str(path),
                line=line_no,
                error=str(exc),
            )
    return cases


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def run_calibration(
    settings: "Settings",
    *,
    gold_set_path: Path,
    engine: "RealRiskEngine | None" = None,
) -> CalibrationReport:
    """
    Run the engine over each gold case and report P@5 + Brier + bins.

    When `len(cases) < settings.gold_set_min_n`, returns a report with metrics
    set to None and gates marked "insufficient_gold_set" — engine is still
    invoked per-case (so smoke testing remains useful) but aggregates are
    suppressed to avoid emitting noisy small-N statistics.

    `engine` may be passed to share an instance across calls (test isolation);
    otherwise a fresh ``RealRiskEngine`` is constructed.
    """
    cases = load_gold_set(gold_set_path)
    n = len(cases)
    insufficient = n < settings.gold_set_min_n

    if engine is None:
        from debouw.risk.engine import RealRiskEngine
        from debouw.risk.narrate import Narrator
        engine = RealRiskEngine(settings, narrator=Narrator(settings))
    assert engine is not None  # for type narrowing
    # Prime per-category query vectors before per-case classify() loop
    # (review-v5 N1 — warmup is hoisted out of classify; without this call
    # the harness measures the no-precedent baseline, not the precedent
    # boost it is meant to validate).
    await engine.warmup()

    # Lazy DB session — only instantiated when we need to load projects.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}")
    Session = async_sessionmaker(db_engine, expire_on_commit=False)

    p5_per_case: list[float] = []
    brier_total = 0.0
    brier_count = 0
    pairs: list[tuple[float, float]] = []

    from sqlalchemy.exc import OperationalError

    from debouw.storage.repository import get_project

    try:
        async with Session() as session:
            for case in cases:
                try:
                    project = await get_project(session, case.project_external_id)
                except OperationalError as exc:
                    log.warning(
                        "calibration_db_unavailable",
                        external_id=case.project_external_id,
                        error=str(exc),
                    )
                    break
                if project is None:
                    log.warning(
                        "calibration_project_missing",
                        external_id=case.project_external_id,
                    )
                    continue

                assessment = await engine.classify(project)
                top_cats = [rf.category for rf in assessment.top_risks]
                predicted: dict[RiskCategory, float] = {
                    rf.category: rf.probability for rf in assessment.top_risks
                }

                p5_per_case.append(_p_at_5_for_case(top_cats, case.expected_top_categories))
                case_total, case_n = _brier_for_case(
                    predicted, case.expected_top_categories
                )
                brier_total += case_total
                brier_count += case_n
                expected_set = set(case.expected_top_categories)
                for cat, prob in predicted.items():
                    pairs.append((prob, 1.0 if cat in expected_set else 0.0))
    finally:
        await db_engine.dispose()

    bins = _calibration_bins(pairs, num_bins=10)

    if insufficient:
        return CalibrationReport(
            n=n,
            p_at_5=None,
            brier=None,
            calibration_bins=bins,
            gates={
                "p_at_5": "insufficient_gold_set",
                "brier": "insufficient_gold_set",
                "calibration": "insufficient_gold_set",
            },
        )

    p_at_5 = sum(p5_per_case) / len(p5_per_case) if p5_per_case else 0.0
    brier = brier_total / brier_count if brier_count > 0 else 0.0

    gates: dict[str, GateStatus] = {
        "p_at_5": "pass" if p_at_5 >= 0.4 else "fail",
        "brier": "pass" if brier <= 0.25 else "fail",
        "calibration": "pass",  # bins are diagnostic only
    }

    return CalibrationReport(
        n=n,
        p_at_5=p_at_5,
        brier=brier,
        calibration_bins=bins,
        gates=gates,
    )
