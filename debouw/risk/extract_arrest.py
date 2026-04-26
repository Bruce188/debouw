"""
Sonnet schema-bound extractor for RvVb arrest PDFs.

Flow per extract():
1. Cache lookup via repository.get_arrest_extraction().
2. Sonnet call with INSTRUCTIONS_EXTRACT system block (schema-bound JSON response).
3. Validate outcome ∈ {6 enum values}; clamp unknown → "andere" + log warning.
4. Validate grounds_used ⊆ RiskCategory enum; drop invalid + log warning.
5. Persist via repository.upsert_arrest_extraction().

Concurrency: caller wraps in asyncio.Semaphore(settings.sonnet_extraction_concurrency).
Tenacity retry: RateLimitError only; 3 attempts, exponential backoff.

No lancedb import — extraction is the pre-embedding step.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from debouw.models.permit import RiskCategory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from debouw.config import Settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = frozenset(
    {"vernietigd", "gedeeltelijk", "verworpen", "onontvankelijk", "afstand", "andere"}
)

# Build closed-list enumeration from RiskCategory for the prompt
_RISK_CATEGORY_VALUES = [c.value for c in RiskCategory]


class ArrestExtraction(BaseModel):
    """Schema-bound Sonnet extraction result for one RvVb arrest."""

    model_config = ConfigDict(extra="forbid")

    arrest_id: str
    decision_date: date
    grounds_used: list[RiskCategory]      # closed list — taxonomy enum
    outcome: Literal[
        "vernietigd", "gedeeltelijk", "verworpen",
        "onontvankelijk", "afstand", "andere"
    ]
    project_facts: str                    # 200-400 chars summary
    decision_excerpt: str                 # verbatim 1-2 sentence ratio decidendi
    extractor_version: str = "0.1"


# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

INSTRUCTIONS_EXTRACT = (
    "Je bent een juridisch analist die RvVb-arresten analyseert. "
    "Extraheer de volgende velden uit de aangeleverde arresttekst en retourneer "
    "uitsluitend een geldig JSON-object met de velden hieronder. "
    "Gebruik uitsluitend Vlaams Nederlands. "
    "Veld 'outcome': één van {vernietigd, gedeeltelijk, verworpen, onontvankelijk, afstand, andere}. "
    "Veld 'grounds_used': een lijst met waarden uitsluitend uit de lijst: "
    + ", ".join(_RISK_CATEGORY_VALUES)
    + ". "
    "Veld 'project_facts': feitenoverzicht 200-400 tekens. "
    "Veld 'decision_excerpt': verbatim 1-2 zinnen ratio decidendi. "
    "Veld 'decision_date': ISO 8601 datum (YYYY-MM-DD). "
    "Indien een veld niet te bepalen is, gebruik dan een lege waarde of lege lijst."
)

_RESPONSE_SCHEMA_HINT = (
    "Geef een JSON-object terug met exact deze velden:\n"
    '{"decision_date": "YYYY-MM-DD", "grounds_used": ["<category_value>", ...], '
    '"outcome": "<outcome>", "project_facts": "<tekst>", "decision_excerpt": "<tekst>"}'
)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ArrestExtractor:
    """
    Sonnet schema-bound extractor with cache + outcome/grounds clamping.

    Concurrency: caller wraps in asyncio.Semaphore(settings.sonnet_extraction_concurrency).
    """

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        s = self._settings
        if s.anthropic_api_key:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
            except Exception as exc:
                log.warning("extractor_anthropic_init_failed", error=str(exc))

    async def extract(
        self,
        session: "AsyncSession | None",
        arrest_id: str,
        pdf_text: str,
    ) -> ArrestExtraction:
        """
        Extract structured data from an arrest PDF text.

        1. Cache lookup (tier 3 resume safety).
        2. Sonnet extraction with retry on RateLimitError.
        3. Outcome/grounds clamping.
        4. Cache persistence.
        """
        extractor_version = self._settings.arrest_extractor_version

        # Tier 3: cache lookup
        if session is not None:
            from debouw.storage.repository import get_arrest_extraction
            try:
                cached = await get_arrest_extraction(session, arrest_id, extractor_version)
                if cached is not None:
                    log.debug("extractor_cache_hit", arrest_id=arrest_id)
                    payload = cached["payload_json"]
                    return ArrestExtraction.model_validate(payload)
            except Exception as exc:
                log.warning("extractor_cache_read_failed", arrest_id=arrest_id, error=str(exc))

        # Extract via Sonnet
        raw = await self._call_sonnet(arrest_id, pdf_text)

        # Clamp outcome
        outcome = raw.get("outcome", "andere")
        if outcome not in _VALID_OUTCOMES:
            log.warning(
                "extractor_outcome_clamped",
                arrest_id=arrest_id,
                original=outcome,
                clamped="andere",
            )
            outcome = "andere"

        # Clamp grounds_used
        raw_grounds = raw.get("grounds_used", [])
        valid_grounds: list[RiskCategory] = []
        for g in raw_grounds:
            try:
                valid_grounds.append(RiskCategory(g))
            except ValueError:
                log.warning(
                    "extractor_ground_dropped",
                    arrest_id=arrest_id,
                    invalid_ground=g,
                )

        # Parse decision_date
        decision_date_raw = raw.get("decision_date", "2000-01-01")
        try:
            decision_date = date.fromisoformat(decision_date_raw)
        except (ValueError, TypeError):
            log.warning(
                "extractor_date_parse_failed",
                arrest_id=arrest_id,
                raw=decision_date_raw,
            )
            decision_date = date(2000, 1, 1)

        extraction = ArrestExtraction(
            arrest_id=arrest_id,
            decision_date=decision_date,
            grounds_used=valid_grounds,
            outcome=outcome,  # type: ignore[arg-type]
            project_facts=raw.get("project_facts", "")[:400],
            decision_excerpt=raw.get("decision_excerpt", "")[:500],
            extractor_version=extractor_version,
        )

        # Persist to cache (review-v5 B5 — mode="json" returns ISO date
        # strings + enum .value automatically; manual key overrides removed
        # so future schema additions stay JSON-safe without writer churn).
        if session is not None:
            from debouw.storage.repository import upsert_arrest_extraction
            try:
                payload = extraction.model_dump(mode="json")
                await upsert_arrest_extraction(
                    session,
                    arrest_id=arrest_id,
                    extractor_version=extractor_version,
                    payload_json=payload,
                    extracted_at=datetime.now(timezone.utc),
                )
            except Exception as exc:
                log.warning("extractor_cache_write_failed", arrest_id=arrest_id, error=str(exc))

        return extraction

    async def _call_sonnet(self, arrest_id: str, pdf_text: str) -> dict:
        """Call Sonnet with retry on RateLimitError. Returns raw dict."""
        if self._client is None:
            log.warning("extractor_no_client", arrest_id=arrest_id)
            return {}

        import anthropic
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        s = self._settings
        # Truncate PDF text to avoid exceeding context (first 6000 chars is enough)
        truncated_text = pdf_text[:6000]

        system_blocks = [
            {
                "type": "text",
                "text": INSTRUCTIONS_EXTRACT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        user_content = (
            _RESPONSE_SCHEMA_HINT
            + "\n\nARREST_ID: "
            + arrest_id
            + "\n\nARRESTTEKST:\n"
            + truncated_text
        )

        @retry(
            retry=retry_if_exception_type(anthropic.RateLimitError),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            reraise=True,
        )
        async def _call() -> dict:
            response = await self._client.messages.create(
                model=s.sonnet_extraction_model,
                max_tokens=1024,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
            )
            text = response.content[0].text if response.content else "{}"
            # Extract JSON object
            start = text.find("{")
            end = text.rfind("}") + 1
            if start < 0 or end <= start:
                log.warning("extractor_no_json_in_response", arrest_id=arrest_id)
                return {}
            return json.loads(text[start:end])

        try:
            result = await _call()
            log.debug("extractor_sonnet_ok", arrest_id=arrest_id)
            return result
        except anthropic.RateLimitError:
            log.warning("extractor_rate_limit_exhausted", arrest_id=arrest_id)
            raise
        except Exception as exc:
            log.warning("extractor_sonnet_failed", arrest_id=arrest_id, error=str(exc))
            return {}
