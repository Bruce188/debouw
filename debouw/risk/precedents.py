"""
LanceDB precedent store for RvVb arrests.

Architecture:
- One LanceDB table "rvvb_arrests" (name configurable via settings.lancedb_arrests_table).
- Schema: arrest_id, decision_date, grounds_used, outcome, project_facts,
          decision_excerpt, vector (float32[3072]), extractor_version.
- Index: IvfPq cosine, num_partitions=16, num_sub_vectors=96 (built at >=256 rows).
- Search: cosine filtered by category.value in grounds_used.

Engine purity contract:
- search() is a pure local-disk read (no network). Permitted inside classify().
- embed_text() / embed_query_for_category() call OpenAI. NOT used in classify().
- The engine pre-computes per-category query vectors at init via
  embed_query_for_category(); classify() only reads the in-memory dict.

Single-writer assumption: concurrent backfill_run() processes will race on
LanceDB writes. LanceDB's native file lock prevents corruption but may block.
Document this in LIMITATIONS.md.

Idempotent upsert (tier 4 resume safety): upsert_arrest() skips if arrest_id
already present for the given extractor_version.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import pyarrow as pa
import structlog
from pydantic import BaseModel, ConfigDict

# Used to validate arrest_id and extractor_version before f-string interpolation
# into LanceDB ``where`` clauses (review-v5 B3 — defense-in-depth even though
# the parser regex blocks single-quotes upstream).
_SAFE_ARREST_ID_RE = re.compile(r"^RVVB\.\w+\.\d{4}\.\d{4}$")
_SAFE_EXTRACTOR_VERSION_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

if TYPE_CHECKING:
    from debouw.config import Settings
    from debouw.risk.extract_arrest import ArrestExtraction

from debouw.models.permit import PrecedentMatch, RiskCategory

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Arrow schema for the LanceDB table
# ---------------------------------------------------------------------------

_ARREST_SCHEMA = pa.schema(
    [
        pa.field("arrest_id", pa.string()),
        pa.field("decision_date", pa.timestamp("us")),
        pa.field("grounds_used", pa.list_(pa.string())),  # RiskCategory.value strings
        pa.field("outcome", pa.string()),
        pa.field("project_facts", pa.string()),
        pa.field("decision_excerpt", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 3072)),
        pa.field("extractor_version", pa.string()),
    ]
)


# ---------------------------------------------------------------------------
# PrecedentHit — search result
# ---------------------------------------------------------------------------

class PrecedentHit(BaseModel):
    """One search result from the LanceDB precedent store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arrest_id: str
    similarity: float           # cosine similarity 0..1
    outcome: str                # one of 6 enum values
    decision_excerpt: str
    grounds_used: list[str]
    decision_date: date


def _hit_to_precedent_match(hit: PrecedentHit) -> PrecedentMatch:
    """Convert PrecedentHit → PrecedentMatch (the Pydantic model schema contract)."""
    return PrecedentMatch(
        precedent_id=hit.arrest_id,
        summary=hit.decision_excerpt[:120],
        similarity=hit.similarity,
        outcome=hit.outcome,
    )


# ---------------------------------------------------------------------------
# LanceDBPrecedentStore
# ---------------------------------------------------------------------------

class LanceDBPrecedentStore:
    """
    LanceDB-backed precedent corpus for RvVb arrests.

    Thread-safety: single-writer; concurrent writes blocked by LanceDB file lock.
    """

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._db = None
        self._table = None
        self._embedder = None  # lazy OpenAI client
        # In-memory query-vector cache {category: vector} populated at engine init.
        self._query_vector_cache: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self):
        """Lazy-connect to LanceDB."""
        if self._db is None:
            import lancedb
            db_path = str(self._settings.lancedb_path)
            self._db = lancedb.connect(db_path)
        return self._db

    def _get_embedder(self):
        """Lazy-init OpenAI client for embeddings."""
        if self._embedder is None:
            import openai
            api_key = self._settings.openai_api_key
            if api_key:
                self._embedder = openai.AsyncOpenAI(api_key=api_key)
            else:
                log.warning("precedent_store_no_openai_key")
        return self._embedder

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------

    def open_or_create_table(self):
        """
        Idempotent: open existing table or create with schema.

        Builds IvfPq cosine index on first ≥256 rows; subsequent inserts reuse.
        Returns the LanceDB Table object.
        """
        db = self._connect()
        table_name = self._settings.lancedb_arrests_table
        existing = db.table_names()

        if table_name in existing:
            self._table = db.open_table(table_name)
        else:
            try:
                self._table = db.create_table(table_name, schema=_ARREST_SCHEMA)
                log.info("precedent_store_table_created", table=table_name)
            except Exception as exc:
                log.warning("precedent_store_create_failed", error=str(exc))
                # Try opening again (race condition on first create)
                try:
                    self._table = db.open_table(table_name)
                except Exception:
                    self._table = None

        return self._table

    def _ensure_table(self):
        """Return the table, creating if needed."""
        if self._table is None:
            self.open_or_create_table()
        return self._table

    def _maybe_build_index(self, table) -> None:
        """Build IvfPq index if row count >= 256 and no index exists yet."""
        try:
            count = table.count_rows()
            if count >= 256:
                # Check if index already exists by trying to list indices
                try:
                    table.create_index(
                        "vector",
                        index_type="IVF_PQ",
                        num_partitions=16,
                        num_sub_vectors=96,
                        metric="cosine",
                        replace=False,
                    )
                    log.info("precedent_store_index_built", rows=count)
                except Exception:
                    # Index likely already exists; not an error
                    pass
        except Exception as exc:
            log.warning("precedent_store_index_check_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_arrest(self, extraction: "ArrestExtraction", vector: list[float]) -> None:
        """
        Idempotent insert by arrest_id + extractor_version (tier 4 resume safety).

        Skips if a row with the same (arrest_id, extractor_version) already exists.
        """
        table = self._ensure_table()
        if table is None:
            log.warning("precedent_store_upsert_skipped_no_table")
            return

        # Validate arrest_id + extractor_version before string-building the
        # LanceDB ``where`` clause (review-v5 B3 — keeps a stray quote from a
        # malformed Sonnet output or a bad Settings override out of the SQL
        # filter, which would otherwise fall through to the broad except and
        # silently re-insert a duplicate row).
        if not _SAFE_ARREST_ID_RE.match(extraction.arrest_id):
            log.warning(
                "precedent_store_upsert_rejected_malformed_arrest_id",
                arrest_id=extraction.arrest_id,
            )
            return
        if not _SAFE_EXTRACTOR_VERSION_RE.match(extraction.extractor_version):
            log.warning(
                "precedent_store_upsert_rejected_malformed_extractor_version",
                extractor_version=extraction.extractor_version,
            )
            return

        # Check if already present
        try:
            existing = table.search().where(
                f"arrest_id = '{extraction.arrest_id}' AND extractor_version = '{extraction.extractor_version}'"
            ).limit(1).to_arrow()
            if len(existing) > 0:
                log.debug("precedent_store_already_present", arrest_id=extraction.arrest_id)
                return
        except Exception:
            pass  # On error, proceed with upsert

        # Convert decision_date to timestamp (microseconds)
        dt = datetime.combine(extraction.decision_date, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )

        row = {
            "arrest_id": extraction.arrest_id,
            "decision_date": dt,
            "grounds_used": [g.value for g in extraction.grounds_used],
            "outcome": extraction.outcome,
            "project_facts": extraction.project_facts,
            "decision_excerpt": extraction.decision_excerpt,
            "vector": [float(v) for v in vector],
            "extractor_version": extraction.extractor_version,
        }

        try:
            table.add([row])
            log.debug("precedent_store_upserted", arrest_id=extraction.arrest_id)
            self._maybe_build_index(table)
        except Exception as exc:
            log.warning(
                "precedent_store_upsert_failed",
                arrest_id=extraction.arrest_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        category: RiskCategory,
        query_vector: list[float],
        *,
        k: int | None = None,
        threshold: float | None = None,
    ) -> list[PrecedentHit]:
        """
        Cosine search filtered by category.value in grounds_used.

        DETERMINISTIC: same vector + same store state → same hit list.
        Pure local-disk read; permitted inside engine.classify().

        Returns hits sorted by similarity desc, thresholded.
        Returns [] when table doesn't exist, is empty, or query_vector is empty.
        """
        if not query_vector:
            return []

        k_val = k if k is not None else self._settings.precedent_search_k
        threshold_val = (
            threshold if threshold is not None else self._settings.precedent_search_threshold
        )

        table = self._ensure_table()
        if table is None:
            return []

        try:
            row_count = table.count_rows()
            if row_count == 0:
                return []
        except Exception:
            return []

        try:
            cat_value = category.value
            # Build filter for category
            # LanceDB where clause: array contains check
            results = (
                table.search(query_vector, vector_column_name="vector")
                .metric("cosine")
                .limit(k_val * 3)  # over-fetch then filter
                .to_arrow()
            )

            hits: list[PrecedentHit] = []
            for i in range(len(results)):
                row = {col: results[col][i].as_py() for col in results.schema.names}
                grounds = row.get("grounds_used", []) or []
                if cat_value not in grounds:
                    continue
                # cosine distance in LanceDB is 1 - similarity for some versions
                # _distance column: lower = more similar
                distance = row.get("_distance", 0.0)
                similarity = max(0.0, min(1.0, 1.0 - float(distance)))
                if similarity < threshold_val:
                    continue

                # Parse decision_date
                dt_val = row.get("decision_date")
                if hasattr(dt_val, "date"):
                    dec_date = dt_val.date()
                elif isinstance(dt_val, str):
                    try:
                        dec_date = datetime.fromisoformat(dt_val).date()
                    except ValueError:
                        dec_date = date(2000, 1, 1)
                else:
                    dec_date = date(2000, 1, 1)

                hits.append(
                    PrecedentHit(
                        arrest_id=row["arrest_id"],
                        similarity=round(similarity, 6),
                        outcome=row.get("outcome", "andere"),
                        decision_excerpt=row.get("decision_excerpt", ""),
                        grounds_used=grounds,
                        decision_date=dec_date,
                    )
                )
                if len(hits) >= k_val:
                    break

            # Sort by similarity desc (deterministic: tiebreak by arrest_id)
            hits.sort(key=lambda h: (-h.similarity, h.arrest_id))
            return hits[:k_val]

        except Exception as exc:
            log.warning("precedent_store_search_failed", category=category.value, error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed_text(self, text: str) -> list[float]:
        """
        OpenAI text-embedding-3-large; tiktoken-trimmed to 8191 tokens.

        Network call — NOT inside engine.classify().
        Used only by backfill_run pipeline and query-vector pre-computation.
        """
        embedder = self._get_embedder()
        if embedder is None:
            log.warning("precedent_store_embed_no_client")
            return []  # empty vector → engine skips LanceDB search (graceful degrade)

        # Trim to 8191 tokens
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model("text-embedding-3-large")
            tokens = enc.encode(text)
            if len(tokens) > 8191:
                tokens = tokens[:8191]
                text = enc.decode(tokens)
        except Exception:
            # tiktoken failure is non-fatal — pass text as-is
            pass

        try:
            response = await embedder.embeddings.create(
                model=self._settings.embedding_model,
                input=text,
            )
            return response.data[0].embedding
        except Exception as exc:
            log.warning("precedent_store_embed_failed", error=str(exc))
            return [0.0] * self._settings.embedding_dim

    async def embed_query_for_category(self, category: RiskCategory) -> list[float]:
        """
        Pre-compute a representative query vector per category.

        CACHED in-memory by category.value. The cache is populated once per
        RealRiskEngine instance at init time; classify() only reads from the cache.
        No network call in classify().
        """
        cat_key = category.value
        if cat_key in self._query_vector_cache:
            return self._query_vector_cache[cat_key]

        # Build a representative query text from the taxonomy label + legal basis
        from debouw.risk.taxonomy import TAXONOMY
        defn = TAXONOMY.get(category)
        if defn is None:
            query_text = cat_key
        else:
            query_text = f"{defn.label_nl} {defn.legal_basis_nl}"

        vector = await self.embed_text(query_text)
        self._query_vector_cache[cat_key] = vector
        return vector
