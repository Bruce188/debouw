"""
Tests for risk/precedents.py — LanceDBPrecedentStore.

Uses a tmp lancedb_path. NO live OpenAI calls — embed_text is mocked.

PLATFORM NOTE: lancedb 0.19.0's native binding segfaults on Python 3.14 even
on a bare ``lancedb.connect(...)`` call. The full LanceDB-touching tests are
skipped on Python 3.14 to keep CI green; the determinism / filter / threshold
contracts are still covered by ``test_engine_precedents.py`` (which goes
through the engine's empty-vector graceful-degrade path) and by manual smoke
testing on Python 3.12 prior to release. See LIMITATIONS.md
"## LanceDB single-writer".
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

from debouw.config import Settings
from debouw.models.permit import RiskCategory
from debouw.risk.extract_arrest import ArrestExtraction
from debouw.risk.precedents import LanceDBPrecedentStore

# Skip the LanceDB-touching tests on Python 3.14+ (native binding segfaults).
pytestmark = pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="lancedb 0.19.0 native binding segfaults on Python 3.14",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key=None,
        openai_api_key=None,
        lancedb_path=tmp_path / "lancedb",
        precedent_search_threshold=0.0,  # accept anything for deterministic tests
    )


def _make_extraction(
    arrest_id: str,
    *,
    grounds: list[RiskCategory],
    outcome: str = "vernietigd",
    decision_date: date | None = None,
) -> ArrestExtraction:
    return ArrestExtraction(
        arrest_id=arrest_id,
        decision_date=decision_date or date(2025, 1, 1),
        grounds_used=grounds,
        outcome=outcome,  # type: ignore[arg-type]
        project_facts=f"Synthetic facts for {arrest_id}",
        decision_excerpt=f"Synthetic excerpt for {arrest_id}",
    )


def _unit_vector(seed: int, dim: int = 3072) -> list[float]:
    """Deterministic synthetic vector. seed picks a single hot dimension."""
    vec = [0.0] * dim
    vec[seed % dim] = 1.0
    return vec


# ---------------------------------------------------------------------------
# Open or create
# ---------------------------------------------------------------------------

def test_open_or_create_table_idempotent(tmp_path):
    """Two calls return tables of the same identity, row count preserved."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    t1 = store.open_or_create_table()
    t2 = store.open_or_create_table()
    assert t1 is not None
    assert t2 is not None
    assert t1.name == t2.name


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------

def test_upsert_arrest_idempotent(tmp_path):
    """Two upserts with the same arrest_id + extractor_version → 1 row."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()
    extraction = _make_extraction("RVVB.A.2425.0001", grounds=[RiskCategory.WATER_FLOOD])
    vector = _unit_vector(1)
    store.upsert_arrest(extraction, vector)
    store.upsert_arrest(extraction, vector)
    table = store._table
    assert table is not None
    assert table.count_rows() == 1


# ---------------------------------------------------------------------------
# Malformed arrest_id rejected (review-v5 B3 — defense in depth)
# ---------------------------------------------------------------------------

def test_upsert_arrest_rejects_malformed_arrest_id(tmp_path):
    """
    upsert_arrest must validate arrest_id against ``RVVB.X.YYYY.NNNN`` before
    interpolating it into the LanceDB ``where`` clause. A stray quote or other
    SQL-meaningful character must trigger an early-return without writing.
    """
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()

    bad = ArrestExtraction(
        arrest_id="RVVB.A.2425.0001'; DROP TABLE precedents; --",
        decision_date=date(2025, 1, 1),
        grounds_used=[RiskCategory.WATER_FLOOD],
        outcome="vernietigd",
        project_facts="x",
        decision_excerpt="x",
        extractor_version="0.1",
    )
    store.upsert_arrest(bad, _unit_vector(1))

    table = store._table
    assert table is not None
    assert table.count_rows() == 0


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------

def test_search_empty_store_returns_empty_list(tmp_path):
    """Fresh store with no rows → search returns []."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()
    hits = store.search(RiskCategory.WATER_FLOOD, _unit_vector(1))
    assert hits == []


def test_search_empty_query_vector_returns_empty(tmp_path):
    """Empty vector → guard returns [] without calling LanceDB."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()
    hits = store.search(RiskCategory.WATER_FLOOD, [])
    assert hits == []


# ---------------------------------------------------------------------------
# Filter by category
# ---------------------------------------------------------------------------

def test_search_filters_by_category(tmp_path):
    """Hits filtered by category.value membership in grounds_used."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()

    # Two arrests with WATER_FLOOD ground, one with HERITAGE_INV only.
    store.upsert_arrest(
        _make_extraction("RVVB.A.2425.0010", grounds=[RiskCategory.WATER_FLOOD]),
        _unit_vector(1),
    )
    store.upsert_arrest(
        _make_extraction("RVVB.A.2425.0011", grounds=[RiskCategory.WATER_FLOOD]),
        _unit_vector(1),
    )
    store.upsert_arrest(
        _make_extraction("RVVB.A.2425.0012", grounds=[RiskCategory.HERITAGE_INV]),
        _unit_vector(1),
    )

    hits = store.search(RiskCategory.WATER_FLOOD, _unit_vector(1))
    ids = {h.arrest_id for h in hits}
    assert "RVVB.A.2425.0010" in ids
    assert "RVVB.A.2425.0011" in ids
    assert "RVVB.A.2425.0012" not in ids


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_search_deterministic(tmp_path):
    """Two consecutive searches return identical hit lists."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()
    for i in range(3):
        store.upsert_arrest(
            _make_extraction(
                f"RVVB.A.2425.{i:04d}", grounds=[RiskCategory.WATER_FLOOD]
            ),
            _unit_vector(i + 1),
        )
    h1 = store.search(RiskCategory.WATER_FLOOD, _unit_vector(1))
    h2 = store.search(RiskCategory.WATER_FLOOD, _unit_vector(1))
    assert [h.arrest_id for h in h1] == [h.arrest_id for h in h2]
    assert [h.similarity for h in h1] == [h.similarity for h in h2]


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

def test_search_respects_threshold(tmp_path):
    """Threshold near 1.0 filters out all but the exact-match vector."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    store.open_or_create_table()
    store.upsert_arrest(
        _make_extraction("exact", grounds=[RiskCategory.WATER_FLOOD]),
        _unit_vector(1),
    )
    store.upsert_arrest(
        _make_extraction("orthogonal", grounds=[RiskCategory.WATER_FLOOD]),
        _unit_vector(2),
    )
    hits = store.search(
        RiskCategory.WATER_FLOOD, _unit_vector(1), threshold=0.99
    )
    ids = {h.arrest_id for h in hits}
    assert "exact" in ids
    assert "orthogonal" not in ids


# ---------------------------------------------------------------------------
# embed_text falls back to [] when no client
# ---------------------------------------------------------------------------

def test_embed_text_returns_empty_without_client(tmp_path):
    """No OPENAI_API_KEY → embed_text returns [] (graceful degrade)."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    vec = asyncio.run(store.embed_text("any text"))
    assert vec == []


# ---------------------------------------------------------------------------
# embed_query_for_category caches per category
# ---------------------------------------------------------------------------

def test_embed_query_for_category_caches(tmp_path):
    """Two calls for the same category hit the cache once."""
    s = _settings(tmp_path)
    store = LanceDBPrecedentStore(s)
    # First call returns [] (no client) and caches []
    v1 = asyncio.run(store.embed_query_for_category(RiskCategory.WATER_FLOOD))
    v2 = asyncio.run(store.embed_query_for_category(RiskCategory.WATER_FLOOD))
    assert v1 == v2
    assert "water_flood" in store._query_vector_cache
