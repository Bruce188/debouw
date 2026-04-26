"""
Tests for risk/narrate.py — mocked Anthropic + OpenAI fallback + static template.

NO live network calls. All API clients are mocked via unittest.mock.AsyncMock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from debouw.config import Settings
from debouw.models.permit import (
    Address,
    GeoOverlays,
    GeoPoint,
    PermitProject,
    PermitProjectStatus,
    RiskCategory,
)
from debouw.risk.narrate import Narrator, ProjectNarration, RiskNarration
from debouw.risk.scoring import ScoredFactor

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)
_POINT = GeoPoint(lat=51.0543, lon=3.7174)


def _project() -> PermitProject:
    return PermitProject(
        external_id="test:narrate_test",
        source="gent_consultatie",
        omv_reference="OMV_NARRATE",
        detail_url="https://gent.consultatieomgeving.net/burger/dossier/OMV_NARRATE",
        title="Narrator test project",
        description=None,
        applicant_name=None,
        address=Address(
            raw="Teststraat 1, 9000 Gent",
            point=_POINT,
            parcel_id=None,
        ),
        project_type=None,
        floors=5,
        height_m=18.0,
        units=20,
        parking_spaces=10,
        trees_to_fell=None,
        mer_status=None,
        iioa_class=None,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=None,
        raw_html_path=Path("/tmp/narrate.html"),
        first_seen_at=_T,
        last_changed_at=_T,
        content_hash="c" * 64,
        decision_regime="post_2026_reform",
    )


def _settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key=None,
        openai_api_key=None,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _factors(confidence: float = 0.8) -> list[ScoredFactor]:
    return [
        ScoredFactor(
            category=RiskCategory.GRO_HEIGHT,
            probability=0.7,
            severity=0.8,
            expected_delay_days=240.0,
            confidence=confidence,
            evidence=["floors=5"],
            typical_objector="omwonenden",
        ),
        ScoredFactor(
            category=RiskCategory.WATER_FLOOD,
            probability=0.5,
            severity=0.9,
            expected_delay_days=270.0,
            confidence=confidence,
            evidence=["in_signaalgebied=True"],
            typical_objector="VMM",
        ),
    ]


def _canned_narration() -> ProjectNarration:
    return ProjectNarration(
        summary_nl="Samenvatting van het risicoprofiel.",
        per_risk={
            "gro_height": RiskNarration(
                rationale_nl="De bouwhoogte overschrijdt de norm.",
                citations=["art. 4.3.1 §2 VCRO"],
                certainty="hoog",
            ),
            "water_flood": RiskNarration(
                rationale_nl="Overstromingsrisico aanwezig.",
                citations=["art. 9/1 Watertoetsbesluit"],
                certainty="midden",
            ),
        },
    )


# ---------------------------------------------------------------------------
# Anthropic primary path
# ---------------------------------------------------------------------------

def test_anthropic_path_returns_parsed_narration():
    """Anthropic mock returns ProjectNarration; messages.create called once."""
    s = _settings(anthropic_api_key="sk-test-anthropic")
    canned = _canned_narration()
    json_response = canned.model_dump_json()

    # Mock the Anthropic client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json_response)]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        narrator = Narrator(s)
        # Manually inject the mock client
        narrator._anthropic_client = mock_client

        import asyncio
        result = asyncio.run(narrator.narrate(None, _project(), _factors()))

    mock_client.messages.create.assert_called_once()
    assert result.summary_nl == canned.summary_nl
    assert "gro_height" in result.per_risk


# ---------------------------------------------------------------------------
# OpenAI fallback path
# ---------------------------------------------------------------------------

def test_openai_fallback_when_anthropic_key_missing():
    """OpenAI fallback fires when anthropic_api_key is None."""
    s = _settings(openai_api_key="sk-test-openai")
    canned = _canned_narration()

    # Mock OpenAI beta.chat.completions.parse
    mock_choice = MagicMock()
    mock_choice.message.parsed = canned
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_openai_client = MagicMock()
    mock_openai_client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)

    with patch("openai.AsyncOpenAI", return_value=mock_openai_client):
        narrator = Narrator(s)
        narrator._openai_client = mock_openai_client
        narrator._anthropic_client = None  # no anthropic

        import asyncio
        result = asyncio.run(narrator.narrate(None, _project(), _factors()))

    mock_openai_client.beta.chat.completions.parse.assert_called_once()
    assert result.summary_nl == canned.summary_nl


# ---------------------------------------------------------------------------
# Static template path
# ---------------------------------------------------------------------------

def test_static_template_when_both_keys_missing():
    """No API keys → all certainty='laag', no API mock invoked."""
    s = _settings()  # both keys None
    narrator = Narrator(s)

    import asyncio
    result = asyncio.run(narrator.narrate(None, _project(), _factors(confidence=0.8)))

    for narr in result.per_risk.values():
        assert narr.certainty == "laag"


# ---------------------------------------------------------------------------
# Low confidence gate
# ---------------------------------------------------------------------------

def test_skips_api_when_all_factors_low_confidence():
    """All factors confidence=0.1 → static path; Anthropic NOT called."""
    s = _settings(anthropic_api_key="sk-test-anthropic")

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    import asyncio
    result = asyncio.run(narrator.narrate(None, _project(), _factors(confidence=0.1)))

    mock_client.messages.create.assert_not_called()
    for narr in result.per_risk.values():
        assert narr.certainty == "laag"


# ---------------------------------------------------------------------------
# Hedged flag in prompt
# ---------------------------------------------------------------------------

def test_hedged_flag_propagates_to_prompt():
    """Factor with confidence=0.4 has hedged:true in user message JSON."""
    s = _settings(anthropic_api_key="sk-test-anthropic")
    canned = _canned_narration()

    captured_messages = []

    async def _capture(*args, **kwargs):
        captured_messages.append(kwargs.get("messages", args[0] if args else []))
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=canned.model_dump_json())]
        return mock_message

    mock_client = MagicMock()
    mock_client.messages.create = _capture

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    import asyncio
    asyncio.run(narrator.narrate(None, _project(), _factors(confidence=0.4)))

    # Find the user message
    assert captured_messages, "No messages were captured"
    msgs = captured_messages[0]
    user_msg_content = next(
        (m["content"] for m in msgs if m["role"] == "user"), None
    )
    assert user_msg_content is not None

    # Find the JSON payload — search for hedged key
    assert '"hedged"' in user_msg_content


# ---------------------------------------------------------------------------
# Cache hit short-circuits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_short_circuits(tmp_engine):
    """Pre-populate cache via upsert_cached; narrate returns cached; Anthropic NOT called."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    s = _settings(anthropic_api_key="sk-test-anthropic", engine_version="0.2.0-rules-v1")
    canned = _canned_narration()

    Session = async_sessionmaker(tmp_engine, expire_on_commit=False)

    # Pre-populate cache
    async with Session() as session:
        from debouw.risk.cache import upsert_cached
        await upsert_cached(session, "test:narrate_test", "0.2.0-rules-v1", canned)
        await session.commit()

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    async with Session() as session:
        result = await narrator.narrate(session, _project(), _factors())

    mock_client.messages.create.assert_not_called()
    assert result.summary_nl == canned.summary_nl


# ---------------------------------------------------------------------------
# Rate limit retry + fall-through
# ---------------------------------------------------------------------------

def test_anthropic_rate_limit_retried_then_falls_through():
    """Anthropic raises RateLimitError thrice → falls through to static (no OpenAI key)."""
    import anthropic

    s = _settings(anthropic_api_key="sk-test-anthropic")

    # Build a mock that always raises RateLimitError
    mock_client = MagicMock()

    async def _raise_rate_limit(*args, **kwargs):
        raise anthropic.RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body={},
        )

    mock_client.messages.create = _raise_rate_limit

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client
    narrator._openai_client = None  # no OpenAI fallback

    import asyncio
    result = asyncio.run(narrator.narrate(None, _project(), _factors()))

    # Should fall through to static template
    for narr in result.per_risk.values():
        assert narr.certainty == "laag"
