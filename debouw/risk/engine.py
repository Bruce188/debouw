"""
RealRiskEngine — Phase 2 deterministic rule classifier + LLM narrator.

Satisfies the RiskEngine Protocol (interface.py) as a drop-in replacement
for StubRiskEngine.

Engine purity contract:
- classify(project) is called by pipeline.py — no HTTP, no LanceDB.
- Narrator network calls live in narrate.py; engine invokes narrate only
  when session_factory is supplied (CLI path) or via a plain async call
  (which still uses cache-bypass when session is None).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from statistics import mean

from sqlalchemy.ext.asyncio import AsyncSession

from debouw.config import Settings
from debouw.models.permit import (
    GeoOverlays,
    PermitProject,
    RiskAssessment,
    RiskCategory,
    RiskFactor,
)
from debouw.risk.features import extract
from debouw.risk.narrate import Narrator, ProjectNarration, _static_narration
from debouw.risk.rules import apply_all
from debouw.risk.scoring import ScoredFactor, aggregate, score_hit, top_k
from debouw.risk.taxonomy import TAXONOMY, get_category_def


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RealRiskEngine:
    """
    Deterministic 14-category rule classifier with optional LLM narrator.

    The session_factory parameter enables cache I/O for narration.
    When None (pipeline.py path), narration runs without cache.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        narrator: Narrator | None = None,
        session_factory: Callable[[], AsyncSession] | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._narrator = narrator if narrator is not None else Narrator(settings)
        self._session_factory = session_factory
        self._now = now
        # Pre-computed parcel_repeat_counts keyed by project.external_id
        # Populated by classify_all() before per-project classify calls.
        self._parcel_repeat_counts: dict[str, int] = {}

    async def classify(self, project: PermitProject) -> RiskAssessment:
        """
        Classify a single project.

        Steps:
        1. Extract features.
        2. Apply all 14 rules.
        3. Score each hit.
        4. Aggregate + top-k.
        5. Narrate (with or without cache depending on session_factory).
        6. Assemble RiskAssessment.
        """
        parcel_repeat_count = self._parcel_repeat_counts.get(project.external_id, 0)

        # 1. Feature extraction
        features = extract(
            project,
            project.overlays,
            parcel_repeat_count=parcel_repeat_count,
        )

        # 2. Apply rules
        hits = apply_all(features, project.overlays, project)

        # 3. Score each hit
        scored: list[ScoredFactor] = []
        for hit in hits:
            defn = get_category_def(hit.category)
            factor = score_hit(hit, defn, features, project, project.overlays)
            scored.append(factor)

        # 4. Aggregate + top-k
        overall_score, expected_delay_days = aggregate(scored)
        top5 = top_k(scored, k=5)

        # 5. Narrate
        if self._session_factory is not None:
            async with self._session_factory() as session:
                narration = await self._narrator.narrate(session, project, top5)
                await session.commit()
        else:
            narration = await self._narrator.narrate(None, project, top5)

        # 6. Assemble RiskFactor list
        top_risks = _build_risk_factors(top5, narration)

        # Inputs hash
        overlay_keys = sorted(type(project.overlays).model_fields.keys() if project.overlays else [])
        inputs_hash = hashlib.sha256(
            (
                project.content_hash
                + self._settings.engine_version
                + ","
                + ",".join(overlay_keys)
            ).encode()
        ).hexdigest()

        # Mean confidence over top5
        mean_confidence = mean(f.confidence for f in top5) if top5 else 0.0

        return RiskAssessment(
            project_external_id=project.external_id,
            overall_score=overall_score,
            expected_delay_days=expected_delay_days,
            confidence=mean_confidence,
            summary=narration.summary_nl,
            top_risks=top_risks,
            engine_version=self._settings.engine_version,
            calibration_regime=project.decision_regime,
            generated_at=self._now(),
            inputs_hash=inputs_hash,
        )


def _build_risk_factors(
    factors: list[ScoredFactor],
    narration: ProjectNarration,
) -> list[RiskFactor]:
    """Zip scored factors with narration entries into RiskFactor models."""
    result: list[RiskFactor] = []
    for sf in factors:
        defn = TAXONOMY[sf.category]
        narr = narration.per_risk.get(sf.category.value)
        rationale = narr.rationale_nl if narr else defn.static_rationale_nl
        result.append(
            RiskFactor(
                category=sf.category,
                label=defn.label_nl,
                rationale=rationale,
                severity=sf.severity,
                probability=sf.probability,
                expected_delay_days=sf.expected_delay_days,
                confidence=sf.confidence,
                typical_objector=defn.typical_objector_template_nl,
                evidence=sf.evidence,
                precedents=[],  # Phase 3+: LanceDB precedent retrieval
            )
        )
    return result


# ---------------------------------------------------------------------------
# classify_all — CLI helper (opens engine + iterates all projects)
# ---------------------------------------------------------------------------

async def classify_all(
    settings: Settings,
    *,
    project_id: str | None = None,
    force: bool = False,
) -> int:
    """
    Classify all (or one) projects in the database.

    Skips projects with an existing assessment at the current engine_version
    unless force=True. Pre-computes parcel_repeat_count per project.
    Returns count of projects classified.
    """
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from debouw.storage.db import make_engine, make_sessionmaker
    from debouw.storage.repository import get_project, upsert_assessment
    from debouw.storage.schema import PermitProjectRow, RiskAssessmentRow

    engine = make_engine(settings)
    Session = make_sessionmaker(engine)
    narrator = Narrator(settings)
    risk_engine = RealRiskEngine(settings, narrator=narrator, session_factory=Session)

    try:
        async with Session() as session:
            # Pre-compute parcel_repeat_counts via GROUP BY on address->parcel_id
            # (SQLite JSON_EXTRACT). Only for projects with parcel_id set.
            try:
                repeat_rows = (
                    await session.execute(
                        text(
                            "SELECT JSON_EXTRACT(address, '$.parcel_id') AS parcel_id, "
                            "COUNT(*) as cnt "
                            "FROM permit_projects "
                            "WHERE JSON_EXTRACT(address, '$.parcel_id') IS NOT NULL "
                            "GROUP BY parcel_id HAVING cnt >= 2"
                        )
                    )
                ).fetchall()
                # Map parcel_id → count; then map external_id → count
                parcel_counts: dict[str, int] = {r[0]: r[1] for r in repeat_rows}

                # Get all projects' external_id + parcel_id
                proj_parcel_rows = (
                    await session.execute(
                        text(
                            "SELECT external_id, JSON_EXTRACT(address, '$.parcel_id') "
                            "FROM permit_projects"
                        )
                    )
                ).fetchall()
                for ext_id, p_id in proj_parcel_rows:
                    if p_id and p_id in parcel_counts:
                        risk_engine._parcel_repeat_counts[ext_id] = parcel_counts[p_id]
            except Exception:
                pass  # parcel counts are advisory; graceful degradation

            # Fetch all project external_ids (or just one)
            q = select(PermitProjectRow.external_id)
            if project_id is not None:
                q = q.where(PermitProjectRow.external_id == project_id)
            rows = (await session.execute(q)).scalars().all()

        count = 0
        for ext_id in rows:
            # Check for existing assessment unless force=True
            if not force:
                async with Session() as session:
                    existing = (
                        await session.execute(
                            select(RiskAssessmentRow).where(
                                RiskAssessmentRow.project_external_id == ext_id,
                                RiskAssessmentRow.engine_version == settings.engine_version,
                            )
                        )
                    ).scalar_one_or_none()
                if existing is not None:
                    continue

            # Load project
            async with Session() as session:
                project = await get_project(session, ext_id)
            if project is None:
                continue

            # Classify
            assessment = await risk_engine.classify(project)

            # Persist
            async with Session() as session:
                async with session.begin():
                    await upsert_assessment(session, assessment)
            count += 1

    finally:
        await engine.dispose()

    return count
