"""
Pytest configuration and shared fixtures for debouw test suite.
"""

from datetime import datetime, date, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from debouw.models.permit import (
    Address,
    GeoOverlays,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
)
from debouw.storage.schema import Base


@pytest.fixture
def tmp_db_url(tmp_path: Path) -> str:
    """Return a SQLite URL for a fresh temporary test database."""
    return f"sqlite+aiosqlite:///{tmp_path}/test.sqlite"


@pytest_asyncio.fixture
async def tmp_engine(tmp_db_url: str):
    """Create a fresh async engine with all tables materialized."""
    engine = create_async_engine(tmp_db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sample_permit_project() -> PermitProject:
    """A fully-populated PermitProject with realistic Belgian values."""
    now = datetime.now(timezone.utc)
    return PermitProject(
        external_id="gent:OMV_TEST_0001",
        source="gent_consultatie",
        omv_reference="OMV_TEST_0001",
        detail_url="https://gent.consultatieomgeving.net/burger/dossier/OMV_TEST_0001",
        title="Test",
        description=None,
        applicant_name=None,
        address=Address(
            raw="Korenmarkt 1, 9000 Gent",
            street="Korenmarkt",
            house_number="1",
            postcode="9000",
            municipality="Gent",
            point=GeoPoint(lat=51.0543, lon=3.7174),
            parcel_id=None,
        ),
        project_type=None,
        floors=None,
        height_m=None,
        units=None,
        parking_spaces=None,
        trees_to_fell=None,
        mer_status=None,
        iioa_class=None,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=None,
        raw_html_path=Path("/tmp/test.html"),
        first_seen_at=now,
        last_changed_at=now,
        content_hash="0" * 64,
        decision_regime="post_2026_reform",
    )
