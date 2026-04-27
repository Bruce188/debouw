"""
Async repository functions for upsert + read.

All functions take session: AsyncSession as the first parameter.
Caller controls transaction scope — no commit() calls inside these functions.
"""

from datetime import datetime, date, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from debouw.models.permit import PermitProject, PublicInquiry, RiskAssessment
from debouw.storage.schema import (
    ArrestExtractionCacheRow,
    PermitProjectRow,
    PublicInquiryRow,
    RiskAssessmentRow,
    RiskNarrationCacheRow,
    RvvbBackfillStateRow,
    ScrapeStateRow,
)


async def upsert_project(session: AsyncSession, project: PermitProject) -> None:
    """INSERT OR REPLACE permit project, JSON-encoding composite fields."""
    # mode="json" would produce ISO strings for datetime/date → rejected by SQLite DateTime.
    # Use mode="python" so datetime/date/Path/HttpUrl stay as native Python objects;
    # SQLAlchemy JSON columns will encode dict/list values via their own JSON type.
    data = project.model_dump(mode="python")
    # Composite/nested fields are stored as JSON; scalar Path → str for raw_html_path
    stmt = sqlite_insert(PermitProjectRow).values(
        external_id=data["external_id"],
        source=data["source"],
        region=data["region"],
        omv_reference=data["omv_reference"],
        detail_url=str(project.detail_url),
        title=data["title"],
        description=data["description"],
        applicant_name=data["applicant_name"],
        address=data["address"],
        project_type=data["project_type"],
        floors=data["floors"],
        height_m=data["height_m"],
        units=data["units"],
        parking_spaces=data["parking_spaces"],
        trees_to_fell=data["trees_to_fell"],
        mer_status=data["mer_status"],
        iioa_class=data["iioa_class"],
        status=data["status"].value if hasattr(data["status"], "value") else data["status"],
        decision_date=project.decision_date,
        decision_outcome=data["decision_outcome"],
        attachments=[str(u) for u in project.attachments],
        dossier_pdfs=[str(p) for p in project.dossier_pdfs],
        overlays=data["overlays"],
        raw_html_path=str(project.raw_html_path),
        first_seen_at=project.first_seen_at,
        last_changed_at=project.last_changed_at,
        content_hash=data["content_hash"],
        decision_regime=data["decision_regime"],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["external_id"],
        set_={
            col: stmt.excluded[col]
            for col in [
                "source", "region", "omv_reference", "detail_url", "title", "description",
                "applicant_name", "address", "project_type", "floors", "height_m",
                "units", "parking_spaces", "trees_to_fell", "mer_status", "iioa_class",
                "status", "decision_date", "decision_outcome", "attachments",
                "dossier_pdfs", "overlays", "raw_html_path", "first_seen_at",
                "last_changed_at", "content_hash", "decision_regime",
            ]
        },
    )
    await session.execute(stmt)


async def get_project(session: AsyncSession, external_id: str) -> PermitProject | None:
    """Fetch a permit project by primary key; returns None on miss."""
    result = await session.execute(
        select(PermitProjectRow).where(PermitProjectRow.external_id == external_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    # Reconstruct from row; composite JSON fields decoded by Pydantic
    row_dict = {
        "external_id": row.external_id,
        "source": row.source,
        "region": row.region,
        "omv_reference": row.omv_reference,
        "detail_url": row.detail_url,
        "title": row.title,
        "description": row.description,
        "applicant_name": row.applicant_name,
        "address": row.address,
        "project_type": row.project_type,
        "floors": row.floors,
        "height_m": row.height_m,
        "units": row.units,
        "parking_spaces": row.parking_spaces,
        "trees_to_fell": row.trees_to_fell,
        "mer_status": row.mer_status,
        "iioa_class": row.iioa_class,
        "status": row.status,
        "decision_date": row.decision_date,
        "decision_outcome": row.decision_outcome,
        "attachments": row.attachments or [],
        "dossier_pdfs": row.dossier_pdfs or [],
        "overlays": row.overlays,
        "raw_html_path": row.raw_html_path,
        # B1 fix: SQLite stores datetimes as naive; re-attach UTC so Pydantic
        # validates correctly and callers get tz-aware datetimes.
        "first_seen_at": (
            row.first_seen_at.replace(tzinfo=timezone.utc)
            if row.first_seen_at and row.first_seen_at.tzinfo is None
            else row.first_seen_at
        ),
        "last_changed_at": (
            row.last_changed_at.replace(tzinfo=timezone.utc)
            if row.last_changed_at and row.last_changed_at.tzinfo is None
            else row.last_changed_at
        ),
        "content_hash": row.content_hash,
        "decision_regime": row.decision_regime,
    }
    return PermitProject.model_validate(row_dict)


async def upsert_assessment(session: AsyncSession, assessment: RiskAssessment) -> None:
    """Upsert a risk assessment; composite key (project_external_id, engine_version)."""
    # mode="python" keeps generated_at as a datetime object (required by SQLite DateTime).
    data = assessment.model_dump(mode="python")
    stmt = sqlite_insert(RiskAssessmentRow).values(
        project_external_id=data["project_external_id"],
        engine_version=data["engine_version"],
        overall_score=data["overall_score"],
        expected_delay_days=data["expected_delay_days"],
        confidence=data["confidence"],
        summary=data["summary"],
        top_risks=data["top_risks"],
        calibration_regime=data["calibration_regime"],
        generated_at=assessment.generated_at,
        inputs_hash=data["inputs_hash"],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_external_id", "engine_version"],
        set_={
            col: stmt.excluded[col]
            for col in [
                "overall_score", "expected_delay_days", "confidence", "summary",
                "top_risks", "calibration_regime", "generated_at", "inputs_hash",
            ]
        },
    )
    await session.execute(stmt)


async def upsert_inquiry(
    session: AsyncSession,
    inquiry: PublicInquiry,
    project_external_id: str | None = None,
) -> None:
    """Upsert a public inquiry; PK is external_id.

    project_external_id must be supplied by Phase 1 callers to correctly link
    the inquiry to its parent PermitProject. When omitted (Phase 0 / test usage),
    the inquiry's own external_id is used as a placeholder FK value.
    """
    # mode="python" keeps period_start/period_end/objection_deadline as date objects
    # (required by SQLite Date type — ISO strings raise TypeError).
    data = inquiry.model_dump(mode="python")
    stmt = sqlite_insert(PublicInquiryRow).values(
        external_id=data["external_id"],
        project_external_id=project_external_id if project_external_id is not None else data["external_id"],
        period_start=inquiry.period_start,
        period_end=inquiry.period_end,
        objection_deadline=inquiry.objection_deadline,
        days_remaining=data["days_remaining"],
        objection_count_known=data["objection_count_known"],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["external_id"],
        set_={
            col: stmt.excluded[col]
            for col in [
                "period_start", "period_end", "objection_deadline",
                "days_remaining", "objection_count_known",
            ]
        },
    )
    await session.execute(stmt)


async def get_scrape_state(
    session: AsyncSession, source: str
) -> tuple[str | None, datetime | None]:
    """Return (cursor, last_run_at) for the given source, or (None, None) if not found."""
    result = await session.execute(
        select(ScrapeStateRow).where(ScrapeStateRow.source == source)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None, None
    return row.cursor, row.last_run_at


async def set_scrape_state(
    session: AsyncSession, source: str, cursor: str | None
) -> None:
    """Upsert scrape cursor + timestamp for the given source."""
    now = datetime.now(timezone.utc)
    stmt = sqlite_insert(ScrapeStateRow).values(
        source=source,
        cursor=cursor,
        last_run_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source"],
        set_={"cursor": stmt.excluded.cursor, "last_run_at": stmt.excluded.last_run_at},
    )
    await session.execute(stmt)


async def get_narration_cache(
    session: AsyncSession,
    project_external_id: str,
    engine_version: str,
) -> dict | None:
    """
    Return the cached narration dict for (project_external_id, engine_version).

    Returns a dict with keys {"rationales_json", "summary", "generated_at"} or
    None on cache miss. Consumed by risk/cache.py::get_cached().
    """
    result = await session.execute(
        select(RiskNarrationCacheRow).where(
            RiskNarrationCacheRow.project_external_id == project_external_id,
            RiskNarrationCacheRow.engine_version == engine_version,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    # Use mode="python" consistent with B1 fix — generated_at stays a datetime
    return {
        "rationales_json": row.rationales_json,
        "summary": row.summary,
        "generated_at": (
            row.generated_at.replace(tzinfo=timezone.utc)
            if row.generated_at and row.generated_at.tzinfo is None
            else row.generated_at
        ),
    }


async def upsert_narration_cache(
    session: AsyncSession,
    project_external_id: str,
    engine_version: str,
    rationales_json: dict,
    summary: str,
    generated_at: datetime,
) -> None:
    """Upsert a narration cache entry; composite PK (project_external_id, engine_version)."""
    stmt = sqlite_insert(RiskNarrationCacheRow).values(
        project_external_id=project_external_id,
        engine_version=engine_version,
        rationales_json=rationales_json,
        summary=summary,
        generated_at=generated_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_external_id", "engine_version"],
        set_={
            col: stmt.excluded[col]
            for col in ["rationales_json", "summary", "generated_at"]
        },
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Arrest extraction cache (tier-3 resume safety)
# ---------------------------------------------------------------------------

async def get_arrest_extraction(
    session: AsyncSession,
    arrest_id: str,
    extractor_version: str,
) -> dict | None:
    """Return cached payload for (arrest_id, extractor_version) or None on miss.

    Returns ``{"payload_json": ..., "extracted_at": ...}``. Used by the RvVb
    Sonnet extractor to short-circuit re-runs (tier 3 resume safety).
    """
    result = await session.execute(
        select(ArrestExtractionCacheRow).where(
            ArrestExtractionCacheRow.arrest_id == arrest_id,
            ArrestExtractionCacheRow.extractor_version == extractor_version,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return {
        "payload_json": row.payload_json,
        "extracted_at": (
            row.extracted_at.replace(tzinfo=timezone.utc)
            if row.extracted_at and row.extracted_at.tzinfo is None
            else row.extracted_at
        ),
    }


async def upsert_arrest_extraction(
    session: AsyncSession,
    *,
    arrest_id: str,
    extractor_version: str,
    payload_json: dict,
    extracted_at: datetime,
) -> None:
    """Upsert an arrest extraction; composite PK (arrest_id, extractor_version)."""
    stmt = sqlite_insert(ArrestExtractionCacheRow).values(
        arrest_id=arrest_id,
        extractor_version=extractor_version,
        payload_json=payload_json,
        extracted_at=extracted_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["arrest_id", "extractor_version"],
        set_={
            col: stmt.excluded[col]
            for col in ["payload_json", "extracted_at"]
        },
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# RvVb backfill state (tier-1 listing-cursor resume safety)
# ---------------------------------------------------------------------------

async def get_rvvb_backfill_state(
    session: AsyncSession,
    source: str = "rvvb",
) -> tuple[int | None, str | None] | None:
    """Return (last_page_processed, last_arrest_id_processed) or None on miss."""
    result = await session.execute(
        select(RvvbBackfillStateRow).where(RvvbBackfillStateRow.source == source)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return row.last_page_processed, row.last_arrest_id_processed


async def upsert_rvvb_backfill_state(
    session: AsyncSession,
    *,
    last_page: int | None,
    last_arrest_id: str | None,
    updated_at: datetime,
    source: str = "rvvb",
) -> None:
    """Upsert the RvVb backfill cursor (one row per source)."""
    stmt = sqlite_insert(RvvbBackfillStateRow).values(
        source=source,
        last_page_processed=last_page,
        last_arrest_id_processed=last_arrest_id,
        updated_at=updated_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source"],
        set_={
            col: stmt.excluded[col]
            for col in ["last_page_processed", "last_arrest_id_processed", "updated_at"]
        },
    )
    await session.execute(stmt)
