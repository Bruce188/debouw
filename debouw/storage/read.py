"""
Sync read seam for Streamlit.

SQLite WAL mode permits multiple concurrent readers + one writer.
This module provides synchronous query helpers — no asyncio event-loop needed
inside Streamlit's synchronous execution model.

All functions return raw dicts; Pydantic validation is NOT applied here.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from debouw.config import Settings


def make_sync_engine(settings: Settings) -> Engine:
    """Build a synchronous SQLAlchemy engine for the debouw SQLite database."""
    return create_engine(
        f"sqlite:///{settings.db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )


def list_projects(engine: Engine) -> list[dict]:
    """Return all permit projects ordered by first_seen_at DESC."""
    sql = text(
        "SELECT external_id, omv_reference, title, address, overlays, status, "
        "first_seen_at FROM permit_projects ORDER BY first_seen_at DESC"
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return [dict(r) for r in rows]


def get_project_with_assessment(
    engine: Engine, external_id: str
) -> tuple[dict | None, dict | None]:
    """Return (project_dict, assessment_dict) for the given external_id.

    Assessment may be None if the risk engine has not yet run.
    """
    proj_sql = text(
        "SELECT external_id, omv_reference, title, address, overlays, status, "
        "first_seen_at, source, description "
        "FROM permit_projects WHERE external_id = :eid"
    )
    asm_sql = text(
        "SELECT project_external_id, overall_score, expected_delay_days, confidence, "
        "summary, top_risks, engine_version "
        "FROM risk_assessments WHERE project_external_id = :eid "
        "ORDER BY rowid DESC LIMIT 1"
    )
    with engine.connect() as conn:
        proj_row = conn.execute(proj_sql, {"eid": external_id}).mappings().first()
        asm_row = conn.execute(asm_sql, {"eid": external_id}).mappings().first()

    project = dict(proj_row) if proj_row else None
    assessment = dict(asm_row) if asm_row else None
    return project, assessment
