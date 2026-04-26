"""
PDF text extraction via pdfplumber.

Async API for consistency with the rest of ingest/ (pdfplumber is sync;
no event-loop blocking concern at Phase 0 scale).
"""

from pathlib import Path

import pdfplumber


async def extract_text(pdf_path: Path) -> str:
    """Extract and concatenate text from all pages of a PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        pages_text = []
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages_text.append(text)
    return "\n\n".join(pages_text)


async def extract_text_with_ocr_fallback(pdf_path: Path) -> str:
    """Extract text; raise NotImplementedError if result is too short (OCR needed).

    Phase 6+: wire pytesseract + pdf2image OCR fallback when triggered.
    """
    text = await extract_text(pdf_path)
    if len(text) < 100:
        raise NotImplementedError(
            "Phase 6+: pytesseract + pdf2image fallback not yet wired"
        )
    return text
