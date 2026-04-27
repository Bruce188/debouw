"""
Migration regression test for 0004_add_region_to_permit_projects.

Verifies that:
1. The migration adds the `region` column with server_default='vl'.
2. Existing rows (inserted WITHOUT a `region` value pre-migration) get
   region='vl' after upgrade — either via server_default or an explicit UPDATE.
3. The downgrade drops the column cleanly.

Uses a temp-dir SQLite file; the DB_PATH env-var overrides Settings().db_path
so the alembic env.py targets the test database.
"""

import os
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


# Alembic revision IDs used in this test
_REV_PRE_0004 = "d8269d3ed50d"   # last revision before the region column
_REV_0004 = "4f9d56aedd52"       # the revision being tested


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    """
    Yield (alembic_cfg, engine) pointing at a fresh temp-dir SQLite DB.

    The DB_PATH env-var override causes Settings() inside alembic/env.py
    to use the temp path, keeping this test fully isolated from any real DB.
    """
    db_path = tmp_path / "migration_test.sqlite"
    # Override the env var that Settings() reads for db_path
    monkeypatch.setenv("DB_PATH", str(db_path))

    cfg = Config("alembic.ini")
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, future=True)
    yield cfg, engine, db_path
    engine.dispose()


def test_region_column_added_with_default(migration_db):
    """
    After applying 0004, the permit_projects table must have a `region` column
    and any pre-existing row must have region='vl'.
    """
    cfg, engine, _ = migration_db

    # Bring DB to the revision just before 0004
    command.upgrade(cfg, _REV_PRE_0004)

    # Insert a minimal permit_projects row WITHOUT specifying region.
    # This simulates a row that existed before Phase 5.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO permit_projects (
                    external_id, source, omv_reference, detail_url, title,
                    address, status, attachments, dossier_pdfs,
                    raw_html_path, first_seen_at, last_changed_at,
                    content_hash, decision_regime
                ) VALUES (
                    'test:pre-migration-row',
                    'gent_consultatie',
                    'OMV_PREMIG_001',
                    'https://example.com/dossier/OMV_PREMIG_001',
                    'Pre-migration test row',
                    '{"raw":"Korenmarkt 1, 9000 Gent"}',
                    'intake',
                    '[]',
                    '[]',
                    '/tmp/OMV_PREMIG_001.html',
                    '2026-04-01T10:00:00',
                    '2026-04-01T10:00:00',
                    'aabbccdd',
                    'post_2026_reform'
                )
                """
            )
        )

    # Verify the row was inserted and region column does NOT yet exist
    with engine.connect() as conn:
        columns = conn.execute(
            text("PRAGMA table_info(permit_projects)")
        ).fetchall()
        col_names = [row[1] for row in columns]
        assert "region" not in col_names, "region column should not exist before 0004"

    # Apply migration 0004
    command.upgrade(cfg, _REV_0004)

    # Verify the column exists now
    with engine.connect() as conn:
        columns = conn.execute(
            text("PRAGMA table_info(permit_projects)")
        ).fetchall()
        col_names = [row[1] for row in columns]
        assert "region" in col_names, "region column must exist after 0004"

    # Verify the pre-migration row has region='vl'
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT region FROM permit_projects "
                "WHERE external_id = 'test:pre-migration-row'"
            )
        ).fetchone()

    assert row is not None, "Pre-migration row not found after upgrade"
    assert row[0] == "vl", (
        f"Pre-migration row should have region='vl' after upgrade, got {row[0]!r}. "
        "If server_default does not populate existing rows on this SQLite version, "
        "the migration should include an explicit UPDATE."
    )


def test_region_column_dropped_on_downgrade(migration_db):
    """Downgrade from 0004 → previous revision must drop the region column."""
    cfg, engine, _ = migration_db

    # Bring DB to 0004
    command.upgrade(cfg, _REV_0004)

    # Verify region column exists
    with engine.connect() as conn:
        columns = conn.execute(
            text("PRAGMA table_info(permit_projects)")
        ).fetchall()
        col_names = [row[1] for row in columns]
        assert "region" in col_names, "region column must exist after upgrade to 0004"

    # Downgrade one step
    command.downgrade(cfg, "-1")

    # Verify region column is gone
    with engine.connect() as conn:
        columns = conn.execute(
            text("PRAGMA table_info(permit_projects)")
        ).fetchall()
        col_names = [row[1] for row in columns]
        assert "region" not in col_names, "region column must be removed after downgrade"
