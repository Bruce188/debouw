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
from debouw.risk.narrate import (
    INSTRUCTIONS,
    INSTRUCTIONS_FR,
    _MIN_NARRATION_MAX_TOKENS,
    Narrator,
    ProjectNarration,
    _ProjectNarrationWire,
    RiskNarration,
    _RiskNarrationEntry,
    _static_narration,
)
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

def _canned_wire() -> _ProjectNarrationWire:
    return _ProjectNarrationWire(
        summary_nl="Samenvatting van het risicoprofiel.",
        per_risk=[
            _RiskNarrationEntry(
                category="gro_height",
                rationale_nl="De bouwhoogte overschrijdt de norm.",
                citations=["art. 4.3.1 §2 VCRO"],
                certainty="hoog",
            ),
            _RiskNarrationEntry(
                category="water_flood",
                rationale_nl="Overstromingsrisico aanwezig.",
                citations=["art. 9/1 Watertoetsbesluit"],
                certainty="midden",
            ),
        ],
    )


def test_openai_fallback_when_anthropic_key_missing():
    """OpenAI fallback fires when anthropic_api_key is None; wire shape folds back."""
    s = _settings(openai_api_key="sk-test-openai")
    wire = _canned_wire()

    # Mock OpenAI beta.chat.completions.parse
    mock_choice = MagicMock()
    mock_choice.message.parsed = wire
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
    # parse() must be sent the strict-compatible wire model, never ProjectNarration
    call_kwargs = mock_openai_client.beta.chat.completions.parse.call_args.kwargs
    assert call_kwargs["response_format"] is _ProjectNarrationWire

    assert result.summary_nl == wire.summary_nl
    # Wire entries fold back into per_risk keyed by category
    assert set(result.per_risk) == {"gro_height", "water_flood"}
    assert result.per_risk["gro_height"].certainty == "hoog"
    assert result.per_risk["water_flood"].citations == ["art. 9/1 Watertoetsbesluit"]


def test_openai_response_format_is_strict_compatible():
    """The schema the SDK derives from the wire model satisfies OpenAI strict mode.

    Strict mode invariants, at EVERY object level (including $defs):
    - 'properties' present and 'required' lists exactly those keys;
    - 'additionalProperties' is literal False (dynamic-key dicts forbidden).
    The old ProjectNarration schema violated this via per_risk: dict[str, ...].
    """
    from openai.lib._pydantic import to_strict_json_schema

    def _assert_strict(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object":
                props = node.get("properties")
                assert props is not None, f"{path}: object without 'properties'"
                assert node.get("additionalProperties") is False, (
                    f"{path}: additionalProperties must be False, got "
                    f"{node.get('additionalProperties')!r}"
                )
                assert sorted(node.get("required", [])) == sorted(props), (
                    f"{path}: 'required' must list every key in 'properties'"
                )
            for key, value in node.items():
                _assert_strict(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _assert_strict(value, f"{path}[{i}]")

    schema = to_strict_json_schema(_ProjectNarrationWire)
    _assert_strict(schema, "$")


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
# Cache write discipline: LLM results only, never static templates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_static_fallback_is_not_cached(tmp_engine):
    """Anthropic fails, no OpenAI → static narration; cache stays empty."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from debouw.risk.cache import get_cached

    s = _settings(anthropic_api_key="sk-test-anthropic", engine_version="0.2.0-rules-v1")

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client
    narrator._openai_client = None  # no OpenAI fallback

    Session = async_sessionmaker(tmp_engine, expire_on_commit=False)
    async with Session() as session:
        result = await narrator.narrate(session, _project(), _factors())
        await session.commit()

    # Static fallback returned
    for narr in result.per_risk.values():
        assert narr.certainty == "laag"

    # Cache must NOT contain the static template
    async with Session() as session:
        cached = await get_cached(session, "test:narrate_test", "0.2.0-rules-v1")
    assert cached is None


@pytest.mark.asyncio
async def test_llm_success_is_cached(tmp_engine):
    """Anthropic succeeds → narration is persisted to the cache."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from debouw.risk.cache import get_cached

    s = _settings(anthropic_api_key="sk-test-anthropic", engine_version="0.2.0-rules-v1")
    canned = _canned_narration()

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=canned.model_dump_json())]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    Session = async_sessionmaker(tmp_engine, expire_on_commit=False)
    async with Session() as session:
        result = await narrator.narrate(session, _project(), _factors())
        await session.commit()

    assert result.summary_nl == canned.summary_nl

    async with Session() as session:
        cached = await get_cached(session, "test:narrate_test", "0.2.0-rules-v1")
    assert cached is not None
    assert cached.summary_nl == canned.summary_nl


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


# ---------------------------------------------------------------------------
# max_tokens truncation: detect stop_reason, retry once with doubled budget
# ---------------------------------------------------------------------------

def _message(text: str, stop_reason: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.stop_reason = stop_reason
    return msg


def test_anthropic_truncation_retries_with_doubled_budget():
    """stop_reason='max_tokens' → one retry at 2x budget; floor is 4096."""
    s = _settings(anthropic_api_key="sk-test-anthropic")
    canned = _canned_narration()

    truncated = _message('{\n  "summary_nl": "Le p', stop_reason="max_tokens")
    complete = _message(canned.model_dump_json(), stop_reason="end_turn")

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[truncated, complete])

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    import asyncio
    result = asyncio.run(narrator.narrate(None, _project(), _factors()))

    assert mock_client.messages.create.call_count == 2
    first_budget = mock_client.messages.create.call_args_list[0].kwargs["max_tokens"]
    second_budget = mock_client.messages.create.call_args_list[1].kwargs["max_tokens"]
    assert first_budget >= _MIN_NARRATION_MAX_TOKENS
    assert second_budget == first_budget * 2

    # Truncated first response was never parsed; retry result is returned
    assert result.summary_nl == canned.summary_nl


@pytest.mark.asyncio
async def test_anthropic_truncated_twice_falls_through_and_is_not_cached(tmp_engine):
    """Both attempts truncated → static fallback, no cache write."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from debouw.risk.cache import get_cached

    s = _settings(anthropic_api_key="sk-test-anthropic", engine_version="0.2.0-rules-v1")

    truncated_1 = _message('{\n  "summary_nl": "Le p', stop_reason="max_tokens")
    truncated_2 = _message('{\n  "summary_nl": "Le proj', stop_reason="max_tokens")

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=[truncated_1, truncated_2])

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client
    narrator._openai_client = None  # no fallback → static

    Session = async_sessionmaker(tmp_engine, expire_on_commit=False)
    async with Session() as session:
        result = await narrator.narrate(session, _project(), _factors())
        await session.commit()

    assert mock_client.messages.create.call_count == 2

    # Static fallback returned, truncated output never surfaced
    for narr in result.per_risk.values():
        assert narr.certainty == "laag"

    # Truncated/static results must never reach the cache
    async with Session() as session:
        cached = await get_cached(session, "test:narrate_test", "0.2.0-rules-v1")
    assert cached is None


# ---------------------------------------------------------------------------
# Phase 5: FR narrator branch (Brussels region)
# ---------------------------------------------------------------------------

def _brussels_project() -> PermitProject:
    """Minimal Brussels project: region='brussels', source=brussels_openpermits."""
    return PermitProject(
        external_id="brussels:01/PU/1984289",
        source="brussels_openpermits",
        region="brussels",
        omv_reference="01/PU/1984289",
        detail_url="https://openpermits.brussels/fr/event/submission/2026/01/PU/1984289",
        title="Rénovation d'un immeuble à appartements",
        description=None,
        applicant_name=None,
        address=Address(
            raw="Avenue Victor et Jules Bertaux 56, 1070 Anderlecht",
            point=GeoPoint(lat=50.8370, lon=4.3144),
            parcel_id=None,
        ),
        project_type=None,
        floors=4,
        height_m=14.0,
        units=12,
        parking_spaces=0,
        trees_to_fell=None,
        mer_status=None,
        iioa_class=None,
        status=PermitProjectStatus.INTAKE,
        decision_date=None,
        decision_outcome=None,
        attachments=[],
        dossier_pdfs=[],
        overlays=None,
        raw_html_path=Path("/tmp/brussels.html"),
        first_seen_at=_T,
        last_changed_at=_T,
        content_hash="d" * 64,
        decision_regime="post_2026_reform",
    )


def _brussels_factors() -> list[ScoredFactor]:
    """Factors for Brussels-applicable categories only (no VL-only)."""
    return [
        ScoredFactor(
            category=RiskCategory.GRO_HEIGHT,
            probability=0.6,
            severity=0.7,
            expected_delay_days=210.0,
            confidence=0.8,
            evidence=["floors=4"],
            typical_objector="voisins",
        ),
    ]


def test_narrate_brussels_uses_fr_instructions():
    """Brussels project → INSTRUCTIONS_FR + French taxonomy in Anthropic system blocks."""
    s = _settings(anthropic_api_key="sk-test-anthropic")
    project = _brussels_project()
    factors = _brussels_factors()

    # Build a canned narration that Anthropic returns
    canned = ProjectNarration(
        summary_nl="Résumé du profil de risque.",
        per_risk={
            "gro_height": RiskNarration(
                rationale_nl="La hauteur du bâtiment dépasse la norme.",
                citations=["art. 175 CoBAT"],
                certainty="hoog",
            ),
        },
    )
    json_response = canned.model_dump_json()

    captured_system: list = []

    async def _capture_call(*args, **kwargs):
        captured_system.extend(kwargs.get("system", []))
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=json_response)]
        return mock_message

    mock_client = MagicMock()
    mock_client.messages.create = _capture_call

    narrator = Narrator(s)
    narrator._anthropic_client = mock_client

    import asyncio
    result = asyncio.run(narrator.narrate(None, project, factors))

    # INSTRUCTIONS_FR must appear in the system blocks
    system_texts = " ".join(block["text"] for block in captured_system)
    assert INSTRUCTIONS_FR[:60] in system_texts, (
        "INSTRUCTIONS_FR was not included in the Anthropic system blocks for a Brussels project"
    )
    # French taxonomy must also be present: 'CoBAT' is a reliable FR token
    assert "CoBAT" in system_texts, (
        "French taxonomy markdown (CoBAT) was not included in system blocks for a Brussels project"
    )
    # Result should parse fine
    assert result.summary_nl == canned.summary_nl


def test_static_narration_fr_fallback():
    """_static_narration uses FR fields for Brussels, NL fields for VL."""
    from debouw.risk.taxonomy import TAXONOMY

    # Pick a category that has FR sibling fields (GRO_HEIGHT is Brussels-applicable)
    vl_factor = ScoredFactor(
        category=RiskCategory.GRO_HEIGHT,
        probability=0.5,
        severity=0.7,
        expected_delay_days=200.0,
        confidence=0.2,
        evidence=[],
        typical_objector="omwonenden",
    )
    bru_factor = ScoredFactor(
        category=RiskCategory.GRO_HEIGHT,
        probability=0.5,
        severity=0.7,
        expected_delay_days=200.0,
        confidence=0.2,
        evidence=[],
        typical_objector="voisins",
    )

    vl_project = _project()
    bru_project = _brussels_project()

    defn = TAXONOMY[RiskCategory.GRO_HEIGHT]
    assert defn.static_rationale_fr is not None, "GRO_HEIGHT must have static_rationale_fr"
    assert defn.legal_basis_fr is not None, "GRO_HEIGHT must have legal_basis_fr"

    # Vlaanderen → NL fields
    nl_result = _static_narration([vl_factor], project=vl_project)
    assert nl_result.per_risk["gro_height"].rationale_nl == defn.static_rationale_nl
    assert nl_result.per_risk["gro_height"].citations == [defn.legal_basis_nl]
    # NL summary
    assert "Onvoldoende" in nl_result.summary_nl

    # Brussels → FR fields
    fr_result = _static_narration([bru_factor], project=bru_project)
    assert fr_result.per_risk["gro_height"].rationale_nl == defn.static_rationale_fr
    assert fr_result.per_risk["gro_height"].citations == [defn.legal_basis_fr]
    # FR summary
    assert "insuffisantes" in fr_result.summary_nl


# ---------------------------------------------------------------------------
# Prompt hardening (narration-quality panel, docs/uat/05-narration-quality.md):
# grounding, raw-artifact ban, RvVb attribution, tone calibration.
# Both system-prompt variants (NL + FR) must carry equivalent instructions.
# ---------------------------------------------------------------------------

def test_system_prompts_restrict_claims_to_provided_facts():
    """Defect 1: both prompts ban invented details and raw-null quoting."""
    # NL: only facts from the input; missing values are 'unknown', never raw nulls
    assert "uitsluitend feiten" in INSTRUCTIONS
    assert "onbekend" in INSTRUCTIONS
    assert "'null'" in INSTRUCTIONS and "'none'" in INSTRUCTIONS
    # FR equivalent
    assert "que des faits" in INSTRUCTIONS_FR
    assert "inconnue" in INSTRUCTIONS_FR
    assert "'null'" in INSTRUCTIONS_FR and "'none'" in INSTRUCTIONS_FR


def test_system_prompts_ban_raw_artifacts_in_prose():
    """Defect 2: both prompts forbid snake_case slugs and key=value fragments."""
    for prompt in (INSTRUCTIONS, INSTRUCTIONS_FR):
        assert "snake_case" in prompt
        # Concrete examples flagged by the panel
        assert "binding_advice_ignored" in prompt
        assert "mer_status=none" in prompt
        assert "height_m=null" in prompt


def test_system_prompts_attribute_precedents_to_rvvb():
    """Defect 3: precedents are RvVb arrests, never Conseil d'Etat / Raad van State."""
    for prompt in (INSTRUCTIONS, INSTRUCTIONS_FR):
        assert "Raad voor Vergunningsbetwistingen" in prompt
        assert "RvVb" in prompt
    # NL: never attributed to the Raad van State
    assert "nooit toe aan de Raad van State" in INSTRUCTIONS
    # FR (Brussels dossiers): analogous Flemish case law, never Conseil d'État
    assert "jurisprudence flamande analogue" in INSTRUCTIONS_FR
    assert "jamais au Conseil d'État" in INSTRUCTIONS_FR


def test_system_prompts_calibrate_tone_to_overall_score():
    """Defect 4: tone is anchored to overall_score with explicit thresholds."""
    for prompt in (INSTRUCTIONS, INSTRUCTIONS_FR):
        assert "overall_score" in prompt
        assert "0.15" in prompt
        assert "0.30" in prompt
    assert "beperkt risico" in INSTRUCTIONS
    assert "risque limité" in INSTRUCTIONS_FR


def test_system_prompts_scope_legal_basis_and_objector_to_category():
    """Forensics follow-up: legal bases come from the taxonomy table only (no
    statute citations when a category has none), objector profiles and taxonomy
    distances stay within their own category, and concrete input values are
    worked into the prose."""
    # NL
    assert "Juridische grondslag" in INSTRUCTIONS
    assert "geen enkel wetsartikel" in INSTRUCTIONS
    assert "nooit toe op een andere categorie" in INSTRUCTIONS
    assert "vertragingsdagen" in INSTRUCTIONS
    # FR
    assert "Base légale" in INSTRUCTIONS_FR
    assert "ne citez aucun article" in INSTRUCTIONS_FR
    assert "vers une autre catégorie" in INSTRUCTIONS_FR
    assert "jours de retard" in INSTRUCTIONS_FR
    # The old pointers referenced per-risk payload fields that do not exist
    assert "legal_basis_nl" not in INSTRUCTIONS
    assert "legal_basis_fr" not in INSTRUCTIONS_FR


def test_build_user_message_includes_overall_score():
    """_build_user_message embeds the rounded overall_score in the payload JSON."""
    narrator = Narrator(_settings())
    msg = narrator._build_user_message(
        _project(), _factors(), None, overall_score=0.075
    )
    payload = json.loads(msg)
    assert payload["overall_score"] == 0.075


def test_overall_score_propagates_to_anthropic_prompt_payload():
    """narrate() computes overall_score = max(probability) and ships it in the user message."""
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
    asyncio.run(narrator.narrate(None, _project(), _factors()))

    assert captured_messages, "No messages were captured"
    msgs = captured_messages[0]
    user_msg_content = next(
        (m["content"] for m in msgs if m["role"] == "user"), None
    )
    assert user_msg_content is not None
    # _factors() max probability is 0.7 (gro_height)
    assert '"overall_score": 0.7' in user_msg_content


def test_overall_score_propagates_to_openai_prompt_payload():
    """The OpenAI fallback user message carries the same overall_score field."""
    s = _settings(openai_api_key="sk-test-openai")
    wire = _canned_wire()

    mock_choice = MagicMock()
    mock_choice.message.parsed = wire
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_openai_client = MagicMock()
    mock_openai_client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)

    narrator = Narrator(s)
    narrator._openai_client = mock_openai_client
    narrator._anthropic_client = None

    import asyncio
    asyncio.run(narrator.narrate(None, _project(), _factors()))

    call_kwargs = mock_openai_client.beta.chat.completions.parse.call_args.kwargs
    user_msg_content = next(
        m["content"] for m in call_kwargs["messages"] if m["role"] == "user"
    )
    assert '"overall_score": 0.7' in user_msg_content
