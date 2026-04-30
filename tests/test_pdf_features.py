"""
Tests for debouw.ingest.pdf_features.extract_pdf_features.

All tests monkeypatch extract_text to avoid needing real PDF files on disk.

Covers:
1. FR pattern extraction: units/floors/iioa_class from canned text.
2. mentions_ongunstig: FR binding-advice pattern.
3. Empty pdf_paths → all-None result, no exception.
4. Lexical sort + first-non-null-wins with two PDFs in reverse-lexical order.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from debouw.ingest.pdf_features import extract_pdf_features


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_extract_text(texts: dict[str, str]):
    """Return an async monkeypatch for extract_text that maps path→text."""
    async def _inner(pdf_path: Path) -> str:
        return texts.get(str(pdf_path), "")
    return _inner


# ---------------------------------------------------------------------------
# 1. FR extraction: units / floors / iioa_class
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fr_units_floors_iioa(monkeypatch) -> None:
    """FR canned text with known signals → correct extracted values."""
    import debouw.ingest.pdf_features as module
    canned = "12 logements répartis sur 5 étages, classe II en IIOA"
    monkeypatch.setattr(module, "extract_text", _fake_extract_text({"/fake/A.pdf": canned}))

    result = await extract_pdf_features([Path("/fake/A.pdf")], lang="fr")
    assert result["units"] == 12, f"Expected 12 got {result['units']}"
    assert result["floors"] == 5, f"Expected 5 got {result['floors']}"
    assert result["iioa_class"] == 2, f"Expected 2 got {result['iioa_class']}"
    assert result["mentions_ongunstig"] is None  # no binding advice in this text


# ---------------------------------------------------------------------------
# 2. FR binding-advice detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fr_mentions_ongunstig(monkeypatch) -> None:
    """FR binding-advice phrase triggers mentions_ongunstig=True."""
    import debouw.ingest.pdf_features as module
    canned = (
        "Suite à l'avis défavorable de la Commission Royale des Monuments, "
        "le projet ne peut être accepté en l'état."
    )
    monkeypatch.setattr(module, "extract_text", _fake_extract_text({"/fake/B.pdf": canned}))

    result = await extract_pdf_features([Path("/fake/B.pdf")], lang="fr")
    assert result["mentions_ongunstig"] is True
    # Units/floors/iioa_class not in this text
    assert result["units"] is None
    assert result["floors"] is None
    assert result["iioa_class"] is None


# ---------------------------------------------------------------------------
# 3. Empty pdf_paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_pdf_paths() -> None:
    """Empty pdf_paths returns all-None result without raising."""
    result = await extract_pdf_features([], lang="fr")
    assert result == {
        "units": None,
        "floors": None,
        "iioa_class": None,
        "mentions_ongunstig": None,
    }


# ---------------------------------------------------------------------------
# 4. Lexical sort + first-non-null-wins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lexical_sort_first_non_null_wins(monkeypatch) -> None:
    """Lexical sort applied; A.pdf processed before B.pdf → A.pdf's units win."""
    import debouw.ingest.pdf_features as module
    # B.pdf has units=20; A.pdf has units=10
    # Lexical sort: A.pdf < B.pdf, so A.pdf is processed first → units=10 wins
    texts = {
        "/fake/A.pdf": "10 wooneenheden over 2 bouwlagen, klasse I",
        "/fake/B.pdf": "20 wooneenheden over 4 bouwlagen, klasse III",
    }
    monkeypatch.setattr(module, "extract_text", _fake_extract_text(texts))

    # Pass paths in reversed order to confirm sort is applied
    result = await extract_pdf_features(
        [Path("/fake/B.pdf"), Path("/fake/A.pdf")], lang="nl"
    )
    assert result["units"] == 10, f"Expected 10 (A.pdf) but got {result['units']}"


# ---------------------------------------------------------------------------
# 5. extract_text failure → graceful skip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_text_failure_graceful(monkeypatch) -> None:
    """extract_text raising an exception → PDF is skipped; result not affected."""
    import debouw.ingest.pdf_features as module

    async def _raising(pdf_path):
        raise RuntimeError("simulated pdfplumber failure")

    monkeypatch.setattr(module, "extract_text", _raising)

    result = await extract_pdf_features([Path("/fake/broken.pdf")], lang="fr")
    # Should not raise; all fields None
    assert result == {
        "units": None,
        "floors": None,
        "iioa_class": None,
        "mentions_ongunstig": None,
    }
