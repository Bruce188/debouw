"""Tests for the size + page guard added to debouw.ingest.pdf.extract_text (Task 2.1)."""

from pathlib import Path

import pytest
import structlog.testing

import debouw.ingest.pdf as pdf_module
from debouw.ingest.pdf import extract_text

_SAMPLE_PDF = Path(__file__).parent / "fixtures" / "vlaanderen_inzage" / "sample_attachment.pdf"


@pytest.mark.asyncio
async def test_extract_text_happy_path():
    """extract_text returns non-empty string for the sample fixture."""
    text = await extract_text(_SAMPLE_PDF)
    assert isinstance(text, str)
    assert len(text) > 0
    assert "Aanvraag" in text or "omgevingsvergunning" in text.lower() or len(text) > 10


@pytest.mark.asyncio
async def test_extract_text_size_cap_returns_empty(monkeypatch):
    """extract_text returns '' and logs pdf_too_large when size cap is breached."""
    monkeypatch.setattr(pdf_module, "_MAX_PDF_SIZE_BYTES", 100)
    with structlog.testing.capture_logs() as logs:
        result = await extract_text(_SAMPLE_PDF)
    assert result == ""
    assert any(e.get("event") == "pdf_too_large" for e in logs)


@pytest.mark.asyncio
async def test_extract_text_page_cap_returns_empty(monkeypatch):
    """extract_text returns '' and logs pdf_too_many_pages when page cap is 0."""
    monkeypatch.setattr(pdf_module, "_MAX_PDF_PAGES", 0)
    with structlog.testing.capture_logs() as logs:
        result = await extract_text(_SAMPLE_PDF)
    assert result == ""
    assert any(e.get("event") == "pdf_too_many_pages" for e in logs)


@pytest.mark.asyncio
async def test_extract_text_missing_file_returns_empty():
    """extract_text returns '' and logs pdf_stat_failed for a non-existent path."""
    missing = Path("/nonexistent_debouw_test.pdf")
    with structlog.testing.capture_logs() as logs:
        result = await extract_text(missing)
    assert result == ""
    assert any(e.get("event") == "pdf_stat_failed" for e in logs)
