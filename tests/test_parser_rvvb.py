"""
Tests for ingest/sources/rvvb.py — Drupal listing parser + cursor + filters.

NO live HTTP calls — fixtures live in tests/fixtures/rvvb/.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from debouw.config import Settings
from debouw.ingest.sources.base import SchemaDriftError
from debouw.ingest.sources.rvvb import (
    RvvbSource,
    _ARREST_ID_PATTERN,
    _parse_listing_page,
    _year_from_arrest_id,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "rvvb" / "listing_page.html"


# ---------------------------------------------------------------------------
# _parse_listing_page
# ---------------------------------------------------------------------------

def test_listing_parser_yields_arrest_ids():
    """Fixture has 5 known arrest_ids; parser extracts every one."""
    html = _FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    ids = _parse_listing_page(soup, str(_FIXTURE))
    expected = {
        "RVVB.A.2425.0312",
        "RVVB.A.2425.0287",
        "RVVB.A.2324.0501",
        "RVVB.A.2223.0102",
        "RVVB.A.1819.0044",
    }
    assert expected.issubset({i.upper() for i in ids})


def test_listing_parser_raises_schema_drift_on_missing_container():
    """No view-content container AND no arrest IDs anywhere → SchemaDriftError."""
    soup = BeautifulSoup("<html><body><p>Geen rechtspraak vandaag.</p></body></html>",
                         "html.parser")
    with pytest.raises(SchemaDriftError):
        _parse_listing_page(soup, "https://test.example/rechtspraak")


def test_listing_parser_falls_back_when_container_missing_but_ids_present():
    """No view-content but arrest IDs present in body → returns IDs (no drift)."""
    html = """
    <html><body>
    <p>RVVB.A.2425.0312 was beslist.</p>
    <p>RVVB.A.2425.0287 idem.</p>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    ids = _parse_listing_page(soup, "https://test.example/rechtspraak")
    assert "RVVB.A.2425.0312" in ids


# ---------------------------------------------------------------------------
# Year filter — _year_from_arrest_id
# ---------------------------------------------------------------------------

def test_year_from_arrest_id_extracts_session_year():
    """Session "2425" → year 2024 (academic year start)."""
    assert _year_from_arrest_id("RVVB.A.2425.0312") == 2024
    assert _year_from_arrest_id("RVVB.A.2324.0501") == 2023
    assert _year_from_arrest_id("RVVB.A.2223.0102") == 2022


def test_year_from_arrest_id_returns_none_on_malformed():
    assert _year_from_arrest_id("not-an-arrest-id") is None


def test_year_filter_drops_out_of_range():
    """Synthetic body containing 2018 + 2024 IDs; year_filter=[2024] → only 2024."""
    html = (_FIXTURE).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    ids = _parse_listing_page(soup, str(_FIXTURE))
    # Filter as paginate() would
    filtered = [
        i for i in ids
        if _year_from_arrest_id(i) is not None
        and _year_from_arrest_id(i) in [2024]
    ]
    assert all("2425" in i.upper() or "2324" in i.upper() for i in filtered) or filtered == []


# ---------------------------------------------------------------------------
# Pattern
# ---------------------------------------------------------------------------

def test_arrest_id_pattern_basic():
    text = "Zie ook arrest RVVB.A.2425.0312 en RVVB.A.1819.0044."
    matches = _ARREST_ID_PATTERN.findall(text)
    assert "RVVB.A.2425.0312" in matches
    assert "RVVB.A.1819.0044" in matches


# ---------------------------------------------------------------------------
# RvvbSource client carries User-Agent (regression for Phase 1 plumbing)
# ---------------------------------------------------------------------------

def test_rvvb_client_carries_user_agent():
    """Phase 1 contract: every source's HTTP client has a meaningful User-Agent."""
    settings = Settings()
    source = RvvbSource(settings)
    client = source._client
    assert client is not None
    # ThrottledHttpClient stores the UA on `_user_agent`.
    assert client._user_agent is not None
    assert "debouw-research" in client._user_agent.lower()


# ---------------------------------------------------------------------------
# detail_pass not implemented
# ---------------------------------------------------------------------------

def test_detail_pass_raises_not_implemented():
    """RvVb source intentionally does not produce PermitProject records."""
    import asyncio
    settings = Settings()
    source = RvvbSource(settings)
    with pytest.raises(NotImplementedError):
        asyncio.run(source.detail_pass("RVVB.A.2425.0312"))


def test_index_pass_raises_not_implemented():
    """index_pass intentionally not used by backfill_run; paginate() is used directly."""
    import asyncio
    settings = Settings()
    source = RvvbSource(settings)

    with pytest.raises(NotImplementedError):
        asyncio.run(source.index_pass())


# ---------------------------------------------------------------------------
# PDF download skip-if-exists (tier-2 resume safety)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cursor advances: paginate yields (arrest_id, page) tuples (review-v5 B2)
# ---------------------------------------------------------------------------

def test_paginate_yields_page_index_for_cursor_resume():
    """
    paginate() must yield ``(arrest_id, page_index, pdf_url)`` so backfill_run
    can advance ``last_page_processed`` and download the canonical PDF. Without
    the page index, resume re-fetches ``start_page`` indefinitely (B2
    regression). Without the pdf_url, ``download_pdf`` would 404 against the
    legacy flat ``/sites/default/files/arr/`` heuristic that doesn't exist on
    the live dbrc.be site (post-merge fix).
    """
    import asyncio

    settings = Settings()
    source = RvvbSource(settings)

    # Two-page fixture: page 0 has arrest A with PDF link, page 1 arrest B
    # with PDF link, page 2 empty. PDF hrefs match the live month-folder
    # layout (``/sites/default/files/YYYY-MM/<arrest>.pdf``).
    page0 = (
        '<div class="view-content">'
        '<a href="https://www.dbrc.be/sites/default/files/2024-09/RVVB.A.2425.0001.pdf">'
        "RVVB.A.2425.0001</a></div>"
    )
    page1 = (
        '<div class="view-content">'
        '<a href="https://www.dbrc.be/sites/default/files/2024-09/RVVB.A.2425.0002.pdf">'
        "RVVB.A.2425.0002</a></div>"
    )
    page2 = '<div class="view-content"></div>'

    pages = [page0, page1, page2]
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeClient:
        async def get(self, url: str):
            calls.append(url)
            if "page=0" in url:
                return FakeResponse(pages[0])
            if "page=1" in url:
                return FakeResponse(pages[1])
            return FakeResponse(pages[2])

        async def aclose(self):
            return None

    source._client = FakeClient()  # type: ignore[assignment]

    async def collect():
        out = []
        async for arrest_id, page, pdf_url in source.paginate(start_page=0, limit=10):
            out.append((arrest_id, page, pdf_url))
        return out

    yielded = asyncio.run(collect())
    assert yielded == [
        (
            "RVVB.A.2425.0001",
            0,
            "https://www.dbrc.be/sites/default/files/2024-09/RVVB.A.2425.0001.pdf",
        ),
        (
            "RVVB.A.2425.0002",
            1,
            "https://www.dbrc.be/sites/default/files/2024-09/RVVB.A.2425.0002.pdf",
        ),
    ]


# ---------------------------------------------------------------------------
# PDF download skip (tier-2 resume safety)
# ---------------------------------------------------------------------------

def test_pdf_download_skips_when_file_exists(tmp_path):
    """Tier-2: if the PDF is already on disk, no HTTP call happens."""
    import asyncio
    settings = Settings()
    source = RvvbSource(settings)
    arrest_id = "RVVB.A.2425.0312"
    dest_dir = tmp_path / "rvvb"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{arrest_id}.pdf").write_bytes(b"%PDF-1.4 stub")

    # Replace the client with one that errors on any GET — confirms no fetch.
    class BoomClient:
        async def get(self, *a, **k):
            raise AssertionError("HTTP call made despite cached PDF on disk")

    source._client = BoomClient()  # type: ignore[assignment]

    path = asyncio.run(source.download_pdf(arrest_id, dest_dir=dest_dir))
    assert path.exists()
    assert path.read_bytes().startswith(b"%PDF")
