"""
LLM narrator for the debouw risk engine.

Flow per narrate():
1. Cache lookup (hit → return immediately, no API call).
2. Per-factor confidence gate: factors with confidence < 0.3 get a static
   template; if ALL factors are below threshold, skip API entirely.
3. Anthropic primary (claude-sonnet-4-5): system prompt with prompt caching;
   tenacity retry on RateLimitError only; ValidationError falls through.
4. OpenAI fallback when Anthropic key absent or falls through.
5. Static-template safety net.
6. On non-static success: write cache.

API discipline:
- ONE Sonnet call per (project, engine_version) pair.
- cache_control={"type":"ephemeral"} on system prompt + taxonomy block.
- max_tokens=1024; tenacity: 3 attempts, exponential backoff.
- NO lancedb import in this module.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict

from debouw.config import Settings
from debouw.models.permit import GeoOverlays, PermitProject, RiskCategory
from debouw.risk.taxonomy import TAXONOMY

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RiskNarration(BaseModel):
    """LLM-generated narration for one risk factor."""

    model_config = ConfigDict(extra="forbid")

    rationale_nl: str
    citations: list[str]
    certainty: Literal["hoog", "midden", "laag"]


class ProjectNarration(BaseModel):
    """LLM-generated narration for a whole project."""

    model_config = ConfigDict(extra="forbid")

    summary_nl: str
    per_risk: dict[str, RiskNarration]  # keys = RiskCategory.value


# ---------------------------------------------------------------------------
# Prompt constants (built once at import)
# ---------------------------------------------------------------------------

INSTRUCTIONS = (
    "Je bent een juridisch-technisch analist voor Belgische omgevingsvergunningen. "
    "Schrijf beknopte risicorationales in Vlaams Nederlands (2-3 zinnen per risico). "
    "Verwijs uitsluitend naar juridische grondslagen die vermeld staan in het 'legal_basis_nl'-veld "
    "van het betreffende risico. Citeer geen andere wetsartikelen. "
    "Gebruik geen anglicismen. Wees objectief en feitelijk. "
    "Geef een samenvattende zin van het totale risicoprofiel in 'summary_nl'."
)


def _build_taxonomy_markdown() -> str:
    lines = ["## Risicotaxonomie\n"]
    for cat, defn in TAXONOMY.items():
        lines.append(f"### {cat.value}: {defn.label_nl}")
        lines.append(f"- **Juridische grondslag:** {defn.legal_basis_nl}")
        lines.append(f"- **Typische bezwaarmaker:** {defn.typical_objector_template_nl}")
        lines.append("")
    return "\n".join(lines)


TAXONOMY_AS_MARKDOWN: str = _build_taxonomy_markdown()

# Confidence threshold: factors below this get static template
_LOW_CONFIDENCE_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# Static template builder
# ---------------------------------------------------------------------------

def _static_narration(
    factors: list,  # list[ScoredFactor]
) -> ProjectNarration:
    """Build a fully-static ProjectNarration from taxonomy templates."""
    per_risk: dict[str, RiskNarration] = {}
    for factor in factors:
        defn = TAXONOMY[factor.category]
        per_risk[factor.category.value] = RiskNarration(
            rationale_nl=defn.static_rationale_nl,
            citations=[defn.legal_basis_nl],
            certainty="laag",
        )
    summary_nl = (
        "Onvoldoende gegevens voor een gedetailleerde risicoanalyse; "
        "verifieer de individuele risicofactoren manueel."
    )
    return ProjectNarration(summary_nl=summary_nl, per_risk=per_risk)


# ---------------------------------------------------------------------------
# Narrator
# ---------------------------------------------------------------------------

class Narrator:
    """
    Orchestrates Anthropic primary → OpenAI fallback → static template.

    Clients inject a session for cache I/O; when session is None, cache
    is bypassed (pipeline.py path — no session available per-classify).
    """

    _warned_no_keys: bool = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._anthropic_client = None
        self._openai_client = None
        self._init_clients()

    def _init_clients(self) -> None:
        s = self._settings
        if s.anthropic_api_key:
            try:
                import anthropic
                self._anthropic_client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
            except Exception as exc:
                log.warning("narrator_anthropic_init_failed", error=str(exc))

        if s.openai_api_key:
            try:
                import openai
                self._openai_client = openai.AsyncOpenAI(api_key=s.openai_api_key)
            except Exception as exc:
                log.warning("narrator_openai_init_failed", error=str(exc))

        if self._anthropic_client is None and self._openai_client is None:
            if not Narrator._warned_no_keys:
                log.warning(
                    "narrator_no_api_keys",
                    message="Both ANTHROPIC_API_KEY and OPENAI_API_KEY absent; using static templates.",
                )
                Narrator._warned_no_keys = True

    async def narrate(
        self,
        session,  # AsyncSession | None
        project: PermitProject,
        factors: list,  # list[ScoredFactor]
    ) -> ProjectNarration:
        """
        Produce Dutch-language narration for the top-k risk factors.

        Cache-first: hits cost zero. Miss → API → static fallback.
        """
        s = self._settings

        # 1. Cache lookup (skip when session is None)
        if session is not None and s.narration_cache_enabled:
            from debouw.risk.cache import get_cached
            try:
                cached = await get_cached(session, project.external_id, s.engine_version)
                if cached is not None:
                    log.debug("narrator_cache_hit", project=project.external_id)
                    return cached
            except Exception as exc:
                log.warning("narrator_cache_read_failed", error=str(exc))

        # 2. Confidence gate: factors with confidence < threshold get static
        low_factors = [f for f in factors if f.confidence < _LOW_CONFIDENCE_THRESHOLD]
        api_factors = [f for f in factors if f.confidence >= _LOW_CONFIDENCE_THRESHOLD]

        # If all factors are low-confidence, skip API entirely
        if not api_factors:
            log.debug("narrator_all_low_confidence", project=project.external_id)
            result = _static_narration(factors)
            if session is not None:
                await self._write_cache(session, project.external_id, result)
            return result

        # 3–5. Try Anthropic → OpenAI → static
        narration: ProjectNarration | None = None

        if self._anthropic_client is not None:
            narration = await self._call_anthropic(project, api_factors)

        if narration is None and self._openai_client is not None:
            narration = await self._call_openai(project, api_factors)

        if narration is None:
            narration = _static_narration(api_factors)

        # Merge low-confidence factors with static templates
        if low_factors:
            merged_per_risk = dict(narration.per_risk)
            for factor in low_factors:
                defn = TAXONOMY[factor.category]
                merged_per_risk[factor.category.value] = RiskNarration(
                    rationale_nl=defn.static_rationale_nl,
                    citations=[defn.legal_basis_nl],
                    certainty="laag",
                )
            narration = ProjectNarration(
                summary_nl=narration.summary_nl,
                per_risk=merged_per_risk,
            )

        # 7. Write cache on non-static success
        if session is not None:
            await self._write_cache(session, project.external_id, narration)

        return narration

    async def _write_cache(
        self, session, project_external_id: str, narration: ProjectNarration
    ) -> None:
        from debouw.risk.cache import upsert_cached
        try:
            await upsert_cached(session, project_external_id, self._settings.engine_version, narration)
        except Exception as exc:
            log.warning("narrator_cache_write_failed", error=str(exc))

    def _build_user_message(
        self, project: PermitProject, factors: list
    ) -> str:
        """Build the user message JSON for the LLM."""
        project_dict = {
            "external_id": project.external_id,
            "title": project.title,
            "description": project.description,
            "floors": project.floors,
            "height_m": project.height_m,
            "units": project.units,
            "parking_spaces": project.parking_spaces,
            "trees_to_fell": project.trees_to_fell,
            "mer_status": project.mer_status,
            "iioa_class": project.iioa_class,
            "address": project.address.raw if project.address else None,
        }

        factors_dict = []
        for f in factors:
            factors_dict.append({
                "category": f.category.value,
                "probability": round(f.probability, 4),
                "severity": round(f.severity, 4),
                "expected_delay_days": round(f.expected_delay_days, 1),
                "confidence": round(f.confidence, 4),
                "evidence": f.evidence,
                "hedged": f.confidence < 0.5,  # hedged flag per plan spec
            })

        return json.dumps(
            {"project": project_dict, "risk_factors": factors_dict},
            ensure_ascii=False,
            indent=2,
        )

    async def _call_anthropic(
        self, project: PermitProject, factors: list
    ) -> ProjectNarration | None:
        """Call Anthropic with prompt caching; retry on RateLimitError."""
        import anthropic
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

        s = self._settings
        user_msg = self._build_user_message(project, factors)

        # System prompt with cache_control on both blocks
        system = [
            {
                "type": "text",
                "text": INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": TAXONOMY_AS_MARKDOWN,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        # Build the expected JSON schema description for structured output
        schema_hint = (
            "Respond with a JSON object matching this schema:\n"
            '{"summary_nl": "string", "per_risk": {"<category_value>": '
            '{"rationale_nl": "string", "citations": ["string"], '
            '"certainty": "hoog"|"midden"|"laag"}}}\n\n'
            "Include an entry in per_risk for each category in the input risk_factors."
        )

        @retry(
            retry=retry_if_exception_type(anthropic.RateLimitError),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            reraise=True,
        )
        async def _call() -> ProjectNarration:
            response = await self._anthropic_client.messages.create(
                model=s.sonnet_model,
                max_tokens=s.narration_max_tokens,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": schema_hint + "\n\n" + user_msg,
                    }
                ],
            )
            # Parse response text as JSON → ProjectNarration
            text = response.content[0].text if response.content else "{}"
            # Find JSON object in response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start < 0 or end <= start:
                raise ValueError("No JSON object in Anthropic response")
            return ProjectNarration.model_validate_json(text[start:end])

        try:
            result = await _call()
            log.debug("narrator_anthropic_ok", project=project.external_id)
            return result
        except anthropic.RateLimitError:
            log.warning(
                "narrator_anthropic_rate_limit_exhausted",
                project=project.external_id,
            )
            return None
        except Exception as exc:
            log.warning(
                "narrator_anthropic_failed",
                project=project.external_id,
                error=str(exc),
            )
            return None

    async def _call_openai(
        self, project: PermitProject, factors: list
    ) -> ProjectNarration | None:
        """Call OpenAI fallback; no retry on non-rate-limit errors."""
        s = self._settings
        user_msg = self._build_user_message(project, factors)
        system_text = INSTRUCTIONS + "\n\n" + TAXONOMY_AS_MARKDOWN

        try:
            response = await self._openai_client.beta.chat.completions.parse(
                model=s.openai_fallback_model,
                messages=[
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_msg},
                ],
                response_format=ProjectNarration,
            )
            result = response.choices[0].message.parsed
            if result is None:
                return None
            log.debug("narrator_openai_ok", project=project.external_id)
            return result
        except Exception as exc:
            log.warning(
                "narrator_openai_failed",
                project=project.external_id,
                error=str(exc),
            )
            return None
