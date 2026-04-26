"""
PDF text extraction via pdfplumber.

Async API for consistency with the rest of ingest/ (pdfplumber is sync;
no event-loop blocking concern at Phase 0 scale).

Phase 4: pre-extraction size + page guard. Module constants set the
caps (NOT Settings — keeps Phase 4 config-clean). Breach = log
warning + return empty string (quality issue, not pipeline failure).
"""

from pathlib import Path

import pdfplumber
import structlog

log = structlog.get_logger(__name__)

_MAX_PDF_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB
_MAX_PDF_PAGES: int = 1000


async def extract_text(pdf_path: Path) -> str:
    """Extract and concatenate text from all pages of a PDF.

    Pre-extraction guard:
    - Reject if file size > _MAX_PDF_SIZE_BYTES (50 MB).
    - Reject if page count > _MAX_PDF_PAGES (1000).
    On reject: log structured warning, return empty string.
    """
    try:
        size_bytes = pdf_path.stat().st_size
    except OSError as exc:
        log.warning("pdf_stat_failed", path=str(pdf_path), error=str(exc))
        return ""

    if size_bytes > _MAX_PDF_SIZE_BYTES:
        log.warning(
            "pdf_too_large",
            path=str(pdf_path),
            size_bytes=size_bytes,
            cap_bytes=_MAX_PDF_SIZE_BYTES,
        )
        return ""

    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) > _MAX_PDF_PAGES:
            log.warning(
                "pdf_too_many_pages",
                path=str(pdf_path),
                pages=len(pdf.pages),
                cap_pages=_MAX_PDF_PAGES,
            )
            return ""
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
