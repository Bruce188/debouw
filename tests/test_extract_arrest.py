"""
Tests for risk/extract_arrest.py — Sonnet schema-bound extractor.

NO live API calls — anthropic.AsyncAnthropic is mocked end-to-end.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from debouw.config import Settings
from debouw.models.permit import RiskCategory
from debouw.risk.extract_arrest import ArrestExtraction, ArrestExtractor


_FIXTURE = Path(__file__).parent / "fixtures" / "rvvb" / "sample_extraction.json"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _fake_response(payload: dict) -> MagicMock:
    """Build an Anthropic-shaped response containing JSON `payload` text."""
    response = MagicMock()
    block = MagicMock()
    block.text = json.dumps(payload)
    response.content = [block]
    return response


def _settings() -> Settings:
    return Settings(anthropic_api_key="sk-test", openai_api_key=None)


def _make_extractor(payload: dict) -> tuple[ArrestExtractor, AsyncMock]:
    """Build an extractor with a mocked Sonnet client returning `payload`."""
    s = _settings()
    extractor = ArrestExtractor(s)
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_fake_response(payload))
    extractor._client = mock_client
    return extractor, mock_client.messages.create


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_anthropic_path_returns_valid_extraction():
    """Mocked Sonnet returns the fixture payload → ArrestExtraction parsed correctly."""
    payload = json.loads(_FIXTURE.read_text())
    extractor, _ = _make_extractor(payload)
    extraction = asyncio.run(extractor.extract(None, payload["arrest_id"], "PDF text"))
    assert extraction.arrest_id == "RVVB.A.2425.0312"
    assert extraction.outcome == "vernietigd"
    assert RiskCategory.NATURE_2000_N in extraction.grounds_used
    assert RiskCategory.WATER_FLOOD in extraction.grounds_used


# ---------------------------------------------------------------------------
# Outcome clamping
# ---------------------------------------------------------------------------

def test_outcome_clamp_unknown_to_andere(caplog):
    """Sonnet returns outcome="onbekend" → clamped to "andere" + warning logged."""
    payload = {
        "decision_date": "2025-01-01",
        "grounds_used": [],
        "outcome": "onbekend",
        "project_facts": "x" * 200,
        "decision_excerpt": "y" * 100,
    }
    extractor, _ = _make_extractor(payload)
    extraction = asyncio.run(extractor.extract(None, "RVVB.A.2425.0001", "txt"))
    assert extraction.outcome == "andere"


# ---------------------------------------------------------------------------
# Grounds clamping
# ---------------------------------------------------------------------------

def test_grounds_clamp_drops_unknown():
    """Unknown grounds dropped, valid ones kept."""
    payload = {
        "decision_date": "2025-01-01",
        "grounds_used": ["water_flood", "completely_made_up_category", "trees_kapverg"],
        "outcome": "vernietigd",
        "project_facts": "x" * 100,
        "decision_excerpt": "y" * 100,
    }
    extractor, _ = _make_extractor(payload)
    extraction = asyncio.run(extractor.extract(None, "RVVB.A.2425.0002", "txt"))
    assert RiskCategory.WATER_FLOOD in extraction.grounds_used
    assert RiskCategory.TREES_KAPVERG in extraction.grounds_used
    assert len(extraction.grounds_used) == 2


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def test_decision_date_falls_back_when_unparseable():
    """Unparseable decision_date → 2000-01-01 sentinel."""
    payload = {
        "decision_date": "totally not a date",
        "grounds_used": [],
        "outcome": "andere",
        "project_facts": "x",
        "decision_excerpt": "y",
    }
    extractor, _ = _make_extractor(payload)
    extraction = asyncio.run(extractor.extract(None, "RVVB.A.2425.0003", "txt"))
    assert extraction.decision_date.year == 2000


# ---------------------------------------------------------------------------
# Cache hit short-circuit (tier-3 resume safety)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_short_circuits(tmp_engine):
    """Pre-populate arrest_extraction_cache → no Sonnet call."""
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from debouw.storage.repository import upsert_arrest_extraction

    s = _settings()
    Session = async_sessionmaker(tmp_engine, expire_on_commit=False)
    extractor = ArrestExtractor(s)

    # Mock client; it must NOT be called.
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()
    extractor._client = mock_client

    # Pre-populate cache row.
    cached_payload = {
        "arrest_id": "RVVB.A.2425.0099",
        "decision_date": "2025-04-01",
        "grounds_used": ["water_flood"],
        "outcome": "vernietigd",
        "project_facts": "from cache",
        "decision_excerpt": "from cache",
        "extractor_version": s.arrest_extractor_version,
    }
    async with Session() as session:
        await upsert_arrest_extraction(
            session,
            arrest_id="RVVB.A.2425.0099",
            extractor_version=s.arrest_extractor_version,
            payload_json=cached_payload,
            extracted_at=datetime.now(timezone.utc),
        )
        await session.commit()

    async with Session() as session:
        extraction = await extractor.extract(session, "RVVB.A.2425.0099", "fresh PDF")

    mock_client.messages.create.assert_not_called()
    assert extraction.outcome == "vernietigd"
    assert extraction.project_facts == "from cache"


# ---------------------------------------------------------------------------
# No client → empty extraction
# ---------------------------------------------------------------------------

def test_no_anthropic_client_returns_default_extraction():
    """No ANTHROPIC_API_KEY → no client → fallback default ArrestExtraction."""
    s = Settings(anthropic_api_key=None, openai_api_key=None)
    extractor = ArrestExtractor(s)
    assert extractor._client is None
    extraction = asyncio.run(extractor.extract(None, "RVVB.A.2425.0010", "txt"))
    # Returned anyway — just empty content
    assert isinstance(extraction, ArrestExtraction)
    assert extraction.outcome == "andere"


# ---------------------------------------------------------------------------
# Sonnet response without JSON → empty dict → default extraction
# ---------------------------------------------------------------------------

def test_sonnet_response_without_json_falls_back():
    """Sonnet returns text without JSON → extractor returns default extraction."""
    s = _settings()
    extractor = ArrestExtractor(s)
    bad_response = MagicMock()
    block = MagicMock()
    block.text = "Sorry, I cannot answer."
    bad_response.content = [block]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=bad_response)
    extractor._client = mock_client
    extraction = asyncio.run(extractor.extract(None, "RVVB.A.2425.0011", "txt"))
    assert isinstance(extraction, ArrestExtraction)
