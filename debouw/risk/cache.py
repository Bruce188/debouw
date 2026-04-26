"""
Thin domain-typed wrappers around the narration cache table.

These functions translate between the SQLAlchemy dict layer (repository.py)
and the Pydantic ProjectNarration type (narrate.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from debouw.storage.repository import get_narration_cache, upsert_narration_cache

if TYPE_CHECKING:
    from debouw.risk.narrate import ProjectNarration


async def get_cached(
    session: AsyncSession,
    project_external_id: str,
    engine_version: str,
) -> "ProjectNarration | None":
    """
    Look up a cached ProjectNarration.

    Returns None on cache miss. Raises ValidationError if the stored JSON
    is malformed (treated as a miss by the caller — narrate.py catches it).
    """
    from debouw.risk.narrate import ProjectNarration

    row = await get_narration_cache(session, project_external_id, engine_version)
    if row is None:
        return None

    # Reconstruct from stored JSON.
    # rationales_json shape: {"per_risk": {cat_value: {rationale_nl, citations, certainty}},
    #                         "summary_nl": "..."}
    payload = {
        "summary_nl": row["summary"],
        "per_risk": row["rationales_json"].get("per_risk", {}),
    }
    return ProjectNarration.model_validate(payload)


async def upsert_cached(
    session: AsyncSession,
    project_external_id: str,
    engine_version: str,
    narration: "ProjectNarration",
) -> None:
    """
    Persist a ProjectNarration into the cache table.

    Serialises per_risk into the rationales_json column as a plain dict.
    """
    # Serialize per_risk to a JSON-safe dict
    rationales_json = {
        "per_risk": narration.model_dump(mode="python")["per_risk"],
    }
    await upsert_narration_cache(
        session=session,
        project_external_id=project_external_id,
        engine_version=engine_version,
        rationales_json=rationales_json,
        summary=narration.summary_nl,
        generated_at=datetime.now(timezone.utc),
    )
