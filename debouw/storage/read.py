"""
Sync read seam for Streamlit.

SQLite WAL mode permits multiple concurrent readers + one writer.
This module provides synchronous query helpers — no asyncio event-loop needed
inside Streamlit's synchronous execution model.

JSON columns (``address``, ``overlays``, ``top_risks``, ``attachments``,
``dossier_pdfs``) are deserialised to native Python ``dict`` / ``list`` here so
the UI can call ``.get()`` directly without a ``json.loads`` per call site.
"""

from __future__ import annotations

import json
from typing import Any

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


def _maybe_json(value: Any) -> Any:
    """Deserialise ``value`` if it's a JSON-encoded string; else return as-is.

    SQLite stores JSON columns as TEXT — SQLAlchemy ``text()`` queries return
    raw strings, not Python objects. ``json.loads`` is wrapped in a try block
    so non-JSON strings (e.g. plain ``status`` text) flow through untouched.
    """
    if isinstance(value, str) and value:
        first = value.lstrip()[:1]
        if first in ("{", "["):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
    return value


_PROJECT_JSON_COLUMNS = ("address", "overlays", "attachments", "dossier_pdfs")
_ASSESSMENT_JSON_COLUMNS = ("top_risks",)


def _hydrate_project(row: dict) -> dict:
    for col in _PROJECT_JSON_COLUMNS:
        if col in row:
            row[col] = _maybe_json(row[col])
    return row


def _hydrate_assessment(row: dict) -> dict:
    for col in _ASSESSMENT_JSON_COLUMNS:
        if col in row:
            row[col] = _maybe_json(row[col])
    return row


def list_projects(engine: Engine) -> list[dict]:
    """Return all permit projects ordered by first_seen_at DESC.

    ``region`` is included so the UI region filter can match without a second
    round-trip; JSON columns are hydrated so ``p['address']['municipality']``
    works directly.
    """
    sql = text(
        "SELECT external_id, omv_reference, title, address, overlays, status, "
        "first_seen_at, region, source, description "
        "FROM permit_projects ORDER BY first_seen_at DESC"
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
    return [_hydrate_project(dict(r)) for r in rows]


def get_project_with_assessment(
    engine: Engine, external_id: str
) -> tuple[dict | None, dict | None]:
    """Return (project_dict, assessment_dict) for the given external_id.

    Assessment may be None if the risk engine has not yet run. Both dicts have
    JSON columns hydrated to native Python objects.
    """
    proj_sql = text(
        "SELECT external_id, omv_reference, title, address, overlays, status, "
        "first_seen_at, source, description, region, attachments, dossier_pdfs, "
        "decision_outcome, decision_date, project_type, trees_to_fell "
        "FROM permit_projects WHERE external_id = :eid"
    )
    asm_sql = text(
        "SELECT project_external_id, overall_score, expected_delay_days, confidence, "
        "summary, top_risks, engine_version, calibration_regime, generated_at "
        "FROM risk_assessments WHERE project_external_id = :eid "
        "ORDER BY rowid DESC LIMIT 1"
    )
    with engine.connect() as conn:
        proj_row = conn.execute(proj_sql, {"eid": external_id}).mappings().first()
        asm_row = conn.execute(asm_sql, {"eid": external_id}).mappings().first()

    project = _hydrate_project(dict(proj_row)) if proj_row else None
    assessment = _hydrate_assessment(dict(asm_row)) if asm_row else None
    return project, assessment
