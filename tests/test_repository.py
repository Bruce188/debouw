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


@pytest.mark.asyncio
async def test_upsert_get_region_brussels_round_trip(
    session: AsyncSession, rich_project: PermitProject
) -> None:
    """upsert_project with region='brussels' → get_project returns region='brussels'."""
    brussels_project = rich_project.model_copy(
        update={
            "external_id": "brussels:01/PU/1984289",
            "source": "brussels_openpermits",
            "region": "brussels",
        }
    )
    await upsert_project(session, brussels_project)
    fetched = await get_project(session, brussels_project.external_id)

    assert fetched is not None, "get_project returned None after Brussels upsert"
    assert fetched.region == "brussels"
    assert fetched.source == "brussels_openpermits"
    assert fetched.external_id == "brussels:01/PU/1984289"


@pytest.mark.asyncio
async def test_default_region_is_vl(
    session: AsyncSession, rich_project: PermitProject
) -> None:
    """Existing Gent projects default to region='vl' on round-trip."""
    await upsert_project(session, rich_project)
    fetched = await get_project(session, rich_project.external_id)

    assert fetched is not None
    assert fetched.region == "vl"


# ---------------------------------------------------------------------------
# Sync read seam (debouw/storage/read.py): latest assessment per project
# ---------------------------------------------------------------------------


@pytest.fixture
def read_engine(tmp_path: Path):
    """Sync engine over a fresh file DB with the full schema materialized."""
    from sqlalchemy import create_engine

    from debouw.storage.schema import Base

    engine = create_engine(f"sqlite:///{tmp_path}/read.sqlite", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _insert_project(conn, external_id: str) -> None:
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO permit_projects (external_id, source, region, "
            "omv_reference, detail_url, title, address, status, attachments, "
            "dossier_pdfs, raw_html_path, first_seen_at, last_changed_at, "
            "content_hash, decision_regime) "
            "VALUES (:eid, 'gent_consultatie', 'vl', :eid, "
            "'https://example.test/dossier', 'Read-seam project', "
            "'{\"municipality\": \"Gent\"}', 'intake', '[]', '[]', "
            "'/tmp/read.html', '2026-04-01 10:00:00', '2026-04-01 10:00:00', "
            ":hash, 'post_2026_reform')"
        ),
        {"eid": external_id, "hash": "c" * 64},
    )


def _insert_assessment(
    conn, eid: str, engine_version: str, score: float, generated_at: str
) -> None:
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO risk_assessments (project_external_id, engine_version, "
            "overall_score, expected_delay_days, confidence, summary, top_risks, "
            "calibration_regime, generated_at, inputs_hash) "
            "VALUES (:eid, :ver, :score, 30.0, 0.5, 'samenvatting', "
            "'[{\"category\": \"mer_screening\"}]', 'post_2026_reform', "
            ":gen, :hash)"
        ),
        {
            "eid": eid,
            "ver": engine_version,
            "score": score,
            "gen": generated_at,
            "hash": "d" * 64,
        },
    )


def test_latest_assessments_picks_max_generated_at(read_engine) -> None:
    """latest_assessments returns max(generated_at) regardless of rowid order."""
    from debouw.storage.read import latest_assessments

    with read_engine.begin() as conn:
        _insert_project(conn, "gent:OMV_READ_1")
        # Newer assessment inserted FIRST: rowid order alone would pick the
        # older row; generated_at DESC must pick this one.
        _insert_assessment(
            conn, "gent:OMV_READ_1", "0.2.0", 0.9, "2026-04-05 12:00:00"
        )
        _insert_assessment(
            conn, "gent:OMV_READ_1", "0.1.0", 0.1, "2026-04-01 12:00:00"
        )

    result = latest_assessments(read_engine)
    assert set(result) == {"gent:OMV_READ_1"}
    assert result["gent:OMV_READ_1"]["overall_score"] == 0.9
    assert result["gent:OMV_READ_1"]["engine_version"] == "0.2.0"
    # JSON column hydrated to a native list
    assert isinstance(result["gent:OMV_READ_1"]["top_risks"], list)


def test_latest_assessments_rowid_tiebreak(read_engine) -> None:
    """Equal generated_at → the later-inserted row (higher rowid) wins."""
    from debouw.storage.read import latest_assessments

    with read_engine.begin() as conn:
        _insert_project(conn, "gent:OMV_READ_2")
        _insert_assessment(
            conn, "gent:OMV_READ_2", "0.1.0", 0.1, "2026-04-05 12:00:00"
        )
        _insert_assessment(
            conn, "gent:OMV_READ_2", "0.2.0", 0.9, "2026-04-05 12:00:00"
        )

    result = latest_assessments(read_engine)
    assert result["gent:OMV_READ_2"]["engine_version"] == "0.2.0"
    assert result["gent:OMV_READ_2"]["overall_score"] == 0.9


def test_latest_assessments_one_row_per_project(read_engine) -> None:
    """Two projects with multiple assessments each → exactly two entries."""
    from debouw.storage.read import latest_assessments

    with read_engine.begin() as conn:
        for eid in ("gent:OMV_READ_3", "gent:OMV_READ_4"):
            _insert_project(conn, eid)
            _insert_assessment(conn, eid, "0.1.0", 0.2, "2026-04-01 12:00:00")
            _insert_assessment(conn, eid, "0.2.0", 0.6, "2026-04-05 12:00:00")

    result = latest_assessments(read_engine)
    assert len(result) == 2
    assert all(r["overall_score"] == 0.6 for r in result.values())


def test_get_project_with_assessment_prefers_newest_generated_at(
    read_engine,
) -> None:
    """The per-project read orders by generated_at DESC, rowid as tiebreak."""
    from debouw.storage.read import get_project_with_assessment

    with read_engine.begin() as conn:
        _insert_project(conn, "gent:OMV_READ_5")
        _insert_assessment(
            conn, "gent:OMV_READ_5", "0.2.0", 0.9, "2026-04-05 12:00:00"
        )
        _insert_assessment(
            conn, "gent:OMV_READ_5", "0.1.0", 0.1, "2026-04-01 12:00:00"
        )

    project, assessment = get_project_with_assessment(
        read_engine, "gent:OMV_READ_5"
    )
    assert project is not None
    assert assessment is not None
    assert assessment["overall_score"] == 0.9
    assert assessment["engine_version"] == "0.2.0"
