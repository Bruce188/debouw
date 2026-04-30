"""
Migration regression test for 85930902ba49_add_brussels_score_diff_columns.

Verifies that:
1. The migration adds error_weight, floor_area_m2, case_language columns as nullable.
2. Existing rows (inserted WITHOUT these columns pre-migration) are still readable
   after upgrade (nullable, so NULL is a valid value).
3. Round-trip: insert row with error_weight=12.5, floor_area_m2=4200.0,
   case_language='fr' → read back → values match.
4. Downgrade drops the three columns cleanly.

Uses a temp-dir SQLite file; the DB_PATH env-var overrides Settings().db_path
so the alembic env.py targets the test database.
"""

import os
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


# Alembic revision IDs used in this test
_REV_PRE_0005 = "4f9d56aedd52"    # last revision before the three columns
_REV_0005 = "85930902ba49"        # the revision being tested


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    """
    Yield (alembic_cfg, engine) pointing at a fresh temp-dir SQLite DB.

    The DB_PATH env-var override causes Settings() inside alembic/env.py
    to use the temp path, keeping this test fully isolated from any real DB.
    """
    db_path = tmp_path / "migration_test.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))

    cfg = Config("alembic.ini")
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, future=True)
    yield cfg, engine, db_path
    engine.dispose()


def _col_names(engine) -> list[str]:
    """Return column names for permit_projects."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(permit_projects)")).fetchall()
    return [row[1] for row in rows]


def test_three_columns_added_as_nullable(migration_db):
    """After applying 85930902ba49, the three new columns exist and are nullable."""
    cfg, engine, _ = migration_db

    # Bring DB to the revision just before this migration
    command.upgrade(cfg, _REV_PRE_0005)

    # Verify columns do NOT exist yet
    cols_before = _col_names(engine)
    for col in ("error_weight", "floor_area_m2", "case_language"):
        assert col not in cols_before, f"Column {col!r} should not exist before migration"

    # Apply migration
    command.upgrade(cfg, _REV_0005)

    # Verify all three columns exist
    cols_after = _col_names(engine)
    for col in ("error_weight", "floor_area_m2", "case_language"):
        assert col in cols_after, f"Column {col!r} must exist after migration"

    # Verify nullable: insert a row without providing the new columns
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO permit_projects (
                    external_id, source, region, omv_reference, detail_url, title,
                    address, status, attachments, dossier_pdfs,
                    raw_html_path, first_seen_at, last_changed_at,
                    content_hash, decision_regime
                ) VALUES (
                    'test:null-cols',
                    'brussels_openpermits',
                    'brussels',
                    '01/PU/0000001',
                    'https://openpermits.brussels/fr/_01/PU/0000001',
                    'Null-column test row',
                    '{"raw":"Rue du Midi 12 1000 Bruxelles"}',
                    'intake',
                    '[]',
                    '[]',
                    '/tmp/null_cols.html',
                    '2026-04-30T10:00:00',
                    '2026-04-30T10:00:00',
                    'ccddee' || 'ffeedd' || 'aabbcc' || 'ffeedd' || 'ccddee' || 'aabbcc' || 'ccddee' || 'ffeedd' || 'aabbcc' || 'ffeedd',
                    'post_2026_reform'
                )
                """
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT error_weight, floor_area_m2, case_language "
                "FROM permit_projects WHERE external_id='test:null-cols'"
            )
        ).fetchone()

    assert row is not None, "Null-column row not found"
    assert row[0] is None, "error_weight should be NULL"
    assert row[1] is None, "floor_area_m2 should be NULL"
    assert row[2] is None, "case_language should be NULL"


def test_round_trip_new_columns(migration_db):
    """Insert row with explicit values for new columns → read back matches."""
    cfg, engine, _ = migration_db

    # Apply migration
    command.upgrade(cfg, _REV_0005)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO permit_projects (
                    external_id, source, region, omv_reference, detail_url, title,
                    address, status, attachments, dossier_pdfs,
                    raw_html_path, first_seen_at, last_changed_at,
                    content_hash, decision_regime,
                    error_weight, floor_area_m2, case_language
                ) VALUES (
                    'test:roundtrip',
                    'brussels_openpermits',
                    'brussels',
                    '01/PU/0000002',
                    'https://openpermits.brussels/fr/_01/PU/0000002',
                    'Round-trip test row',
                    '{"raw":"Chaussée de Waterloo 12 1180 Uccle"}',
                    'intake',
                    '[]',
                    '[]',
                    '/tmp/roundtrip.html',
                    '2026-04-30T11:00:00',
                    '2026-04-30T11:00:00',
                    'deadbeef' || 'cafebabe' || 'deadbeef' || 'cafebabe' || 'deadbeef' || 'cafebabe' || 'deadbeef' || 'cafe',
                    'post_2026_reform',
                    12.5,
                    4200.0,
                    'fr'
                )
                """
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT error_weight, floor_area_m2, case_language "
                "FROM permit_projects WHERE external_id='test:roundtrip'"
            )
        ).fetchone()

    assert row is not None, "Round-trip row not found"
    assert abs(row[0] - 12.5) < 1e-6, f"error_weight mismatch: {row[0]}"
    assert abs(row[1] - 4200.0) < 1e-6, f"floor_area_m2 mismatch: {row[1]}"
    assert row[2] == "fr", f"case_language mismatch: {row[2]!r}"


def test_columns_dropped_on_downgrade(migration_db):
    """Downgrade from 85930902ba49 → previous revision must drop the three columns."""
    cfg, engine, _ = migration_db

    # Bring DB to the migration being tested
    command.upgrade(cfg, _REV_0005)

    # Verify columns exist
    cols = _col_names(engine)
    for col in ("error_weight", "floor_area_m2", "case_language"):
        assert col in cols, f"Column {col!r} must exist after upgrade"

    # Downgrade one step
    command.downgrade(cfg, "-1")

    # Verify all three columns are gone
    cols_after = _col_names(engine)
    for col in ("error_weight", "floor_area_m2", "case_language"):
        assert col not in cols_after, f"Column {col!r} must be removed after downgrade"
