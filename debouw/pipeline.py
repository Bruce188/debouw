"""
Pipeline orchestrator: ingest → geocode → enrich → classify → persist.

Wraps the per-dossier loop in a CircuitBreaker to abort on sustained failures.
intel I1 (Path A): project_external_id is passed explicitly to upsert_inquiry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from debouw.config import Settings
from debouw.ingest.circuit_breaker import CircuitBreaker
from debouw.ingest.enrich_geopunt import enrich
from debouw.ingest.geocode import geocode
from debouw.ingest.pdf_features import extract_pdf_features
from debouw.ingest.sources import SchemaDriftError
from debouw.ingest.sources.brussels import BrusselsSource
from debouw.ingest.sources.gent import GentSource
from debouw.ingest.sources.inzageloket import InzageloketSource
from debouw.risk.engine import RealRiskEngine
from debouw.storage.db import make_engine, make_sessionmaker
from debouw.storage.repository import (
    get_project,
    get_scrape_state,
    set_scrape_state,
    upsert_assessment,
    upsert_inquiry,
    upsert_project,
)

log = structlog.get_logger(__name__)

_SOURCE_REGISTRY = {
    "gent": GentSource,
    "vlaanderen_inzage": InzageloketSource,
    "brussels": BrusselsSource,
}


@dataclass
class PipelineResult:
    ingested: int = 0
    overlays: int = 0
    assessments: int = 0
    circuit_open: bool = False


async def run(source: str, *, limit: int | None = None) -> PipelineResult:
    """Run the ingestion pipeline for the named source.

    Returns a PipelineResult with counts of ingested projects, overlays, and
    assessments. Sets circuit_open=True if the circuit breaker tripped.
    """
    if source not in _SOURCE_REGISTRY:
        raise ValueError(
            f"Unknown source '{source}'. Known: {list(_SOURCE_REGISTRY)}"
        )

    settings = Settings()
    engine = make_engine(settings)
    Session = make_sessionmaker(engine)
    breaker = CircuitBreaker()
    risk_engine = RealRiskEngine(settings, session_factory=Session)
    # Prime per-category query vectors before the per-project hot path
    # (review-v5 N1 — warmup is hoisted out of classify; without this call
    # production runs in degraded mode with modifier=1.0 across the board).
    await risk_engine.warmup()
    result = PipelineResult()

    try:
        SourceClass = _SOURCE_REGISTRY[source]
        async with SourceClass(settings) as src:
            async with Session() as s:
                async with s.begin():
                    _cursor, _ = await get_scrape_state(s, source)

            count = 0
            async for uuid in src.index_pass(limit=limit):
                ok, reason = breaker.can_execute()
                if not ok:
                    log.warning(
                        "pipeline_circuit_open",
                        source=source,
                        reason=reason,
                        ingested=result.ingested,
                    )
                    result.circuit_open = True
                    break

                try:
                    project_no_overlay, inquiry = await src.detail_pass(uuid)
                except SchemaDriftError as exc:
                    log.error("pipeline_schema_drift", source=source, error=str(exc))
                    breaker.record_failure()
                    continue
                except Exception as exc:
                    log.error("pipeline_detail_failed", source=source, error=str(exc))
                    breaker.record_failure()
                    continue

                # B3: idempotency gate — skip geocode + enrich + classify when
                # the dossier is unchanged (content_hash matches existing row).
                async with Session() as s:
                    async with s.begin():
                        existing = await get_project(s, project_no_overlay.external_id)

                if (
                    existing is not None
                    and existing.content_hash == project_no_overlay.content_hash
                ):
                    log.info(
                        "pipeline_dossier_unchanged",
                        external_id=project_no_overlay.external_id,
                    )
                    continue

                point = await geocode(project_no_overlay.address.raw, settings)
                overlays = await enrich(point, settings)

                # Extract features from cached PDFs (post-enrich, for latency)
                case_language = project_no_overlay.case_language or "fr"
                pdf_features = await extract_pdf_features(
                    project_no_overlay.dossier_pdfs, lang=case_language
                )

                # Merge PDF-mined values: explicit JSON values take priority;
                # only fill when the project's existing attribute is None.
                pdf_mentions = pdf_features.get("mentions_ongunstig")
                pdf_units = pdf_features.get("units")
                pdf_floors = pdf_features.get("floors")
                pdf_iioa = pdf_features.get("iioa_class")

                # Build merged description (append region-agnostic binding-advice
                # tokens if PDF mentions adverse advice). "ongunstig advies" matches
                # _ONGUNSTIG_VL_RE / _ONGUNSTIG_NL_BR_RE; "avis défavorable" matches
                # _ONGUNSTIG_FR_RE. Appending both lets the region-conditional regex
                # set in features.extract() pick up the signal for any region.
                merged_description = project_no_overlay.description
                _ONGUNSTIG_TOKENS = "ongunstig advies avis défavorable"
                if pdf_mentions and merged_description is not None:
                    merged_description = merged_description + " " + _ONGUNSTIG_TOKENS
                elif pdf_mentions and merged_description is None:
                    merged_description = _ONGUNSTIG_TOKENS

                project = project_no_overlay.model_copy(
                    update={
                        "overlays": overlays,
                        "address": project_no_overlay.address.model_copy(
                            update={"point": point}
                        ),
                        # Prefer explicit values; fill from PDF only when project field is None
                        "units": project_no_overlay.units if project_no_overlay.units is not None else pdf_units,
                        "floors": project_no_overlay.floors if project_no_overlay.floors is not None else pdf_floors,
                        "iioa_class": project_no_overlay.iioa_class if project_no_overlay.iioa_class is not None else pdf_iioa,
                        "description": merged_description,
                        # Preserve first_seen_at from the existing row to avoid clobber
                        **(
                            {"first_seen_at": existing.first_seen_at}
                            if existing is not None
                            else {}
                        ),
                    }
                )

                assessment = await risk_engine.classify(project)

                async with Session() as s:
                    async with s.begin():
                        await upsert_project(s, project)
                        await upsert_assessment(s, assessment)
                        if inquiry is not None:
                            await upsert_inquiry(
                                s,
                                inquiry,
                                project_external_id=project.external_id,  # Path A intel I1
                            )

                breaker.record_success()
                result.ingested += 1
                result.overlays += 1
                result.assessments += 1
                count += 1

                if count % 5 == 0:
                    log.info(
                        "pipeline_progress",
                        source=source,
                        ingested=result.ingested,
                    )

            # Persist state (Gent has no cursor — write last_run_at only)
            async with Session() as s:
                async with s.begin():
                    await set_scrape_state(s, source, cursor=None)

    finally:
        await engine.dispose()

    log.info(
        "pipeline_done",
        source=source,
        ingested=result.ingested,
        overlays=result.overlays,
        assessments=result.assessments,
        circuit_open=result.circuit_open,
    )
    return result
