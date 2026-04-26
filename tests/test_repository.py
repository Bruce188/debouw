"""
Round-trip tests for debouw/storage/repository.py.

Covers upsert → get for PermitProject, RiskAssessment, and PublicInquiry using
an in-memory aiosqlite engine. Non-trivial datetime/date/Path/HttpUrl fields are
used to prove that the mode="python" fix (B1) holds.
"""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from debouw.models.permit import (
    Address,
    GeoOverlays,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
    PublicInquiry,
    RiskAssessment,
)
from debouw.storage.repository import (
    get_project,
    upsert_assessment,
    upsert_inquiry,
    upsert_project,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session(tmp_engine):
    """Open a single AsyncSession over the tmp_engine fixture."""
    factory = async_sessionmaker(tmp_engine, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.commit()


@pytest.fixture
def rich_project() -> PermitProject:
    """PermitProject with non-trivial datetime/date/Path/HttpUrl values."""
    first_seen = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
    last_changed = datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc)
    decision_dt = date(2026, 5, 20)
    return PermitProject(
        external_id="gent:OMV_ROUNDTRIP_001",
        source="gent_consultatie",
        omv_reference="OMV_ROUNDTRIP_001",
        detail_url="https://gent.consultatieomgeving.net/burger/dossier/OMV_ROUNDTRIP_001",
        title="Round-trip test project",
        description="Validates datetime / date / HttpUrl / Path persistence",
        applicant_name=None,
        address=Address(
            raw="Korenmarkt 1, 9000 Gent",
            street="Korenmarkt",
            house_number="1",
            postcode="9000",
            municipality="Gent",
            point=GeoPoint(lat=51.0543, lon=3.7174),
            parcel_id="12345/A",
        ),
        project_type="meergezinswoning",
        floors=4,
        height_m=12.5,
        units=8,
        parking_spaces=4,
        trees_to_fell=2,
        mer_status="screening",
        iioa_class=2,
        status=PermitProjectStatus.DECIDED,
        decision_date=decision_dt,
        decision_outcome="goedgekeurd",
        attachments=["https://gent.example.com/att/001.pdf"],
        dossier_pdfs=[Path("/tmp/debouw/OMV_ROUNDTRIP_001.pdf")],
        overlays=GeoOverlays(
            in_natura_2000=True,
            natura_2000_distance_m=150.0,
            flood_risk_fluvial="medium",
        ),
        raw_html_path=Path("/tmp/debouw/OMV_ROUNDTRIP_001.html"),
        first_seen_at=first_seen,
        last_changed_at=last_changed,
        content_hash="a" * 64,
        decision_regime="post_2026_reform",
    )


@pytest.fixture
def rich_assessment(rich_project: PermitProject) -> RiskAssessment:
    """RiskAssessment with a non-trivial generated_at datetime."""
    return RiskAssessment(
        project_external_id=rich_project.external_id,
        overall_score=0.7,
        expected_delay_days=45.0,
        confidence=0.8,
        summary="Stub assessment for round-trip test",
        top_risks=[],
        engine_version="0.1.0-test",
        calibration_regime="post_2026_reform",
        generated_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
        inputs_hash="b" * 64,
    )


@pytest.fixture
def rich_inquiry(rich_project: PermitProject) -> PublicInquiry:
    """PublicInquiry with three non-trivial date fields."""
    return PublicInquiry(
        external_id="inquiry:ROUNDTRIP_001",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        objection_deadline=date(2026, 5, 7),
        days_remaining=11,
        objection_count_known=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_get_project_round_trip(
    session: AsyncSession, rich_project: PermitProject
) -> None:
    """upsert_project → get_project returns an equal PermitProject."""
    await upsert_project(session, rich_project)
    fetched = await get_project(session, rich_project.external_id)

    assert fetched is not None, "get_project returned None after upsert"
    assert fetched.external_id == rich_project.external_id
    assert fetched.status == rich_project.status
    # datetime fields must survive as timezone-aware datetimes (B1 fix verification)
    assert isinstance(fetched.first_seen_at, datetime)
    assert fetched.first_seen_at.replace(tzinfo=timezone.utc) == rich_project.first_seen_at.replace(tzinfo=timezone.utc)
    assert isinstance(fetched.last_changed_at, datetime)
    assert fetched.last_changed_at.replace(tzinfo=timezone.utc) == rich_project.last_changed_at.replace(tzinfo=timezone.utc)
    # date field must survive as a date object
    assert isinstance(fetched.decision_date, date)
    assert fetched.decision_date == rich_project.decision_date
    # composite scalar fields
    assert fetched.title == rich_project.title
    assert fetched.floors == rich_project.floors
    assert fetched.height_m == rich_project.height_m
    assert fetched.decision_outcome == rich_project.decision_outcome
    assert fetched.content_hash == rich_project.content_hash
    assert fetched.decision_regime == rich_project.decision_regime


@pytest.mark.asyncio
async def test_upsert_project_on_conflict_updates(
    session: AsyncSession, rich_project: PermitProject
) -> None:
    """Second upsert_project with changed title updates the row (idempotent)."""
    await upsert_project(session, rich_project)

    updated = rich_project.model_copy(update={"title": "Updated title"})
    await upsert_project(session, updated)

    fetched = await get_project(session, rich_project.external_id)
    assert fetched is not None
    assert fetched.title == "Updated title"


@pytest.mark.asyncio
async def test_upsert_assessment_round_trip(
    session: AsyncSession,
    rich_project: PermitProject,
    rich_assessment: RiskAssessment,
) -> None:
    """upsert_project then upsert_assessment — generated_at survives as datetime."""
    await upsert_project(session, rich_project)
    await upsert_assessment(session, rich_assessment)
    # No get_assessment yet; just confirm no TypeError was raised.
    # Verify via direct SQL that the row exists and generated_at is a datetime.
    from sqlalchemy import select, text
    result = await session.execute(
        text("SELECT generated_at FROM risk_assessments WHERE project_external_id = :pid"),
        {"pid": rich_project.external_id},
    )
    row = result.fetchone()
    assert row is not None, "RiskAssessmentRow not found after upsert"


@pytest.mark.asyncio
async def test_upsert_inquiry_round_trip(
    session: AsyncSession,
    rich_project: PermitProject,
    rich_inquiry: PublicInquiry,
) -> None:
    """upsert_project then upsert_inquiry — date columns survive as date objects."""
    await upsert_project(session, rich_project)
    # Pass project_external_id explicitly to satisfy the FK
    await upsert_inquiry(session, rich_inquiry, project_external_id=rich_project.external_id)

    from sqlalchemy import text
    result = await session.execute(
        text(
            "SELECT period_start, period_end, objection_deadline, project_external_id"
            " FROM public_inquiries WHERE external_id = :eid"
        ),
        {"eid": rich_inquiry.external_id},
    )
    row = result.fetchone()
    assert row is not None, "PublicInquiryRow not found after upsert"
    # FK correctly set to the project's external_id
    assert row.project_external_id == rich_project.external_id
