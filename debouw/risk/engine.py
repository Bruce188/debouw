"""
RealRiskEngine — Phase 3 deterministic rule classifier + LLM narrator + LanceDB precedents.

Satisfies the RiskEngine Protocol (interface.py) as a drop-in replacement
for StubRiskEngine.

Engine purity contract:
- classify(project) is called by pipeline.py — no HTTP, no LanceDB WRITE.
- LanceDB READ inside classify() is permitted (local-disk, deterministic).
- Anthropic/OpenAI imports live in narrate.py and extract_arrest.py only.
- One-shot async _ensure_query_vectors() populates in-memory query-vector
  cache at first classify() call. After first call, all classify() calls are
  pure local-disk + CPU. Verified by test_engine_classify_no_network_after_init.

Phase 2 regression:
- Empty LanceDB → precedent_modifier=1.0 → scoring byte-identical to Phase 2.
- Regression-locked by tests/test_engine_precedents.py::test_empty_lancedb_yields_phase2_scores.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from statistics import mean

from sqlalchemy.ext.asyncio import AsyncSession

from debouw.config import Settings
from debouw.models.permit import (
    GeoOverlays,
    PermitProject,
    PrecedentMatch,
    RiskAssessment,
    RiskCategory,
    RiskFactor,
)
from debouw.risk.features import extract
from debouw.risk.narrate import Narrator, ProjectNarration, _static_narration
from debouw.risk.precedents import LanceDBPrecedentStore, PrecedentHit
from debouw.risk.rules import apply_all
from debouw.risk.scoring import ScoredFactor, aggregate, score_hit, top_k
from debouw.risk.taxonomy import TAXONOMY, get_category_def


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RealRiskEngine:
    """
    Deterministic 14-category rule classifier with optional LLM narrator.

    Phase 3 additions:
    - LanceDBPrecedentStore for cosine precedent retrieval post-top_k.
    - _ensure_query_vectors() one-shot async populates per-category query vectors
      at first classify() call; subsequent calls are pure local-disk.
    - Two-pass scoring: pass 1 (no precedents) → top_k candidates → LanceDB
      search → pass 2 (with precedent_modifier).

    The session_factory parameter enables cache I/O for narration.
    When None (pipeline.py path), narration runs without cache.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        narrator: Narrator | None = None,
        session_factory: Callable[[], AsyncSession] | None = None,
        precedent_store: LanceDBPrecedentStore | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._narrator = narrator if narrator is not None else Narrator(settings)
        self._session_factory = session_factory
        self._precedent_store = (
            precedent_store if precedent_store is not None
            else LanceDBPrecedentStore(settings)
        )
        self._now = now
        # Pre-computed parcel_repeat_counts keyed by project.external_id
        # Populated by classify_all() before per-project classify calls.
        self._parcel_repeat_counts: dict[str, int] = {}
        # Per-category query vectors populated by _ensure_query_vectors().
        # Empty until first classify() call; pure read-only after that.
        self._query_vectors_by_category: dict[RiskCategory, list[float]] = {}
        self._query_vectors_built: bool = False

    async def warmup(self) -> None:
        """
        One-shot warmup: build per-category query vectors from the taxonomy.

        MUST be called before the first ``classify()`` invocation when an
        OpenAI key is configured. ``classify_all()`` calls this automatically
        before its per-project loop. Direct ``classify(project)`` callers
        are responsible for calling ``await engine.warmup()`` once after
        construction.

        Engine-purity contract (review-v5 B4): this is the ONLY method that
        may call OpenAI. ``classify()`` is a pure local-disk read after
        warmup completes. If the warmup is skipped or OpenAI is unavailable,
        the cache remains empty and ``search()`` returns [] (graceful
        degradation — Phase 2-compatible behaviour).

        The 14 embedding calls are issued in parallel via ``asyncio.gather``
        for cold-start latency parity (review-v5 perf #5).
        """
        if self._query_vectors_built:
            return

        categories = list(RiskCategory)
        try:
            vectors = await asyncio.gather(
                *[
                    self._precedent_store.embed_query_for_category(c)
                    for c in categories
                ],
                return_exceptions=True,
            )
        except Exception:
            self._query_vectors_built = True
            return

        for category, vector in zip(categories, vectors):
            if isinstance(vector, BaseException):
                continue
            if vector and len(vector) == self._settings.embedding_dim:
                self._query_vectors_by_category[category] = vector

        # Flag flipped only after the gather completes — a transient OpenAI
        # outage no longer permanently disables the cache for this instance
        # (review-v5 N1).
        self._query_vectors_built = True

    async def classify(self, project: PermitProject) -> RiskAssessment:
        """
        Classify a single project.

        Steps:
        1. Extract features.
        2. Apply all 14 rules.
        3. PASS 1: Score each hit (no precedent_hits) to establish top_k candidates.
        4. top_k.
        5. LanceDB search per top-5 category (pure local-disk read).
        6. PASS 2: Rescore top-5 with precedent_modifier wired in.
        7. Re-sort after modifier.
        8. Narrate (with precedents_by_category).
        9. Assemble RiskAssessment.

        Engine-purity contract (review-v5 B4): no Anthropic / OpenAI calls
        inside this method. Per-category query vectors must already be
        primed via ``await engine.warmup()`` (or left empty for the
        graceful-degrade path). LanceDB reads are local-disk only.
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

        # 4. PASS 1: Score each hit without precedent_hits
        scored_pass1: list[ScoredFactor] = []
        for hit in hits:
            defn = get_category_def(hit.category)
            factor = score_hit(hit, defn, features, project, project.overlays)
            scored_pass1.append(factor)

        # 5. top_k (initial ranking without precedents)
        top5_pass1 = top_k(scored_pass1, k=5)

        # 6. LanceDB search per top-5 category
        precedents_by_category: dict[RiskCategory, list[PrecedentHit]] = {}
        for sf in top5_pass1:
            qv = self._query_vectors_by_category.get(sf.category)
            if qv:
                hits_for_cat = self._precedent_store.search(
                    sf.category,
                    qv,
                    k=self._settings.precedent_search_k,
                    threshold=self._settings.precedent_search_threshold,
                )
            else:
                hits_for_cat = []
            precedents_by_category[sf.category] = hits_for_cat

        # 7. PASS 2: Rescore top-5 with precedent_modifier
        # Build a lookup from category → original RiskHit (needed for score_hit)
        hits_by_category = {h.category: h for h in hits}
        top5_rescored: list[ScoredFactor] = []
        for sf in top5_pass1:
            original_hit = hits_by_category.get(sf.category)
            if original_hit is None:
                top5_rescored.append(sf)
                continue
            defn = get_category_def(sf.category)
            sf2 = score_hit(
                original_hit,
                defn,
                features,
                project,
                project.overlays,
                precedent_hits=precedents_by_category.get(sf.category),
            )
            top5_rescored.append(sf2)

        # 8. Re-sort after modifier
        top5 = top_k(top5_rescored, k=5)

        # 9. Narrate (passes precedents_by_category for citation in Dutch rationale)
        if self._session_factory is not None:
            async with self._session_factory() as session:
                narration = await self._narrator.narrate(
                    session,
                    project,
                    top5,
                    precedents_by_category=precedents_by_category,
                )
                await session.commit()
        else:
            narration = await self._narrator.narrate(
                None,
                project,
                top5,
                precedents_by_category=precedents_by_category,
            )

        # 10. Assemble RiskFactor list (populate .precedents from LanceDB hits)
        top_risks = _build_risk_factors(top5, narration, precedents_by_category)

        # Aggregate over ALL pass-1 scored factors (full 14-category picture)
        overall_score, expected_delay_days = aggregate(scored_pass1)

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
    precedents_by_category: dict[RiskCategory, list[PrecedentHit]] | None = None,
) -> list[RiskFactor]:
    """Zip scored factors with narration entries into RiskFactor models.

    Populates RiskFactor.precedents with up to 2 PrecedentMatch objects
    per category (for Streamlit display).
    """
    result: list[RiskFactor] = []
    prec_map = precedents_by_category or {}
    for sf in factors:
        defn = TAXONOMY[sf.category]
        narr = narration.per_risk.get(sf.category.value)
        rationale = narr.rationale_nl if narr else defn.static_rationale_nl

        # Convert top-2 PrecedentHit → PrecedentMatch for the schema
        hits = prec_map.get(sf.category, [])[:2]
        precedent_matches: list[PrecedentMatch] = [
            PrecedentMatch(
                precedent_id=h.arrest_id,
                summary=h.decision_excerpt[:120],
                similarity=h.similarity,
                outcome=h.outcome,
            )
            for h in hits
        ]

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
                precedents=precedent_matches,
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
    precedent_store = LanceDBPrecedentStore(settings)
    risk_engine = RealRiskEngine(
        settings,
        narrator=narrator,
        session_factory=Session,
        precedent_store=precedent_store,
    )

    # Engine-purity contract (review-v5 B4): warmup is the ONLY method allowed
    # to call OpenAI. Run it once before the per-project classify() loop so
    # classify() itself stays a pure local-disk read.
    await risk_engine.warmup()

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
