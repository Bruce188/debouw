"""
PDF feature extraction for Brussels dossiers.

Mines units/floors/iioa_class/mentions_ongunstig from cached dossier PDFs.
Called AFTER enrich() in pipeline.py to avoid adding latency to the scrape path.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from debouw.ingest.pdf import extract_text

log = structlog.get_logger(__name__)

# FR/NL regex sets for units/floors/iioa_class (same as brussels.py helpers)
_FLOORS_FR_RE = re.compile(
    r"(\d{1,2})\s*(?:étages?|niveaux|R\+(\d+))",
    re.IGNORECASE | re.MULTILINE,
)
_FLOORS_NL_RE = re.compile(
    r"(\d{1,2})\s*(?:verdiepingen|bouwlagen)",
    re.IGNORECASE | re.MULTILINE,
)
_UNITS_FR_RE = re.compile(
    r"(\d{1,3})\s*(?:logements?|appartements?|unités?\s+de\s+logement)",
    re.IGNORECASE | re.MULTILINE,
)
_UNITS_NL_RE = re.compile(
    r"(\d{1,3})\s*(?:woningen|appartementen|wooneenheden)",
    re.IGNORECASE | re.MULTILINE,
)
_IIOA_FR_RE = re.compile(
    r"classe\s+(I{1,3}|[1-3])\b",
    re.IGNORECASE | re.MULTILINE,
)
_IIOA_NL_RE = re.compile(
    r"klasse\s+(I{1,3}|[1-3])\b",
    re.IGNORECASE | re.MULTILINE,
)
_ROMAN_TO_INT = {"I": 1, "II": 2, "III": 3}

# Binding-advice patterns (broader — PDF text is richer than portal description)
_ONGUNSTIG_FR_RE = re.compile(
    r"\b(avis\s+d[eé]favorable|Bruxelles\s+Environnement|CRMS|Commission\s+Royale\s+des\s+Monuments)\b",
    re.IGNORECASE,
)
_ONGUNSTIG_NL_BR_RE = re.compile(
    r"\b(ongunstig\s+advies|Leefmilieu\s+Brussel|KCML)\b",
    re.IGNORECASE,
)

_EMPTY_RESULT: dict[str, int | bool | None] = {
    "units": None,
    "floors": None,
    "iioa_class": None,
    "mentions_ongunstig": None,
}


async def extract_pdf_features(
    pdf_paths: list[Path], lang: str
) -> dict[str, int | bool | None]:
    """
    Extract feature signals from a list of cached PDF files.

    Returns a dict with keys: units, floors, iioa_class, mentions_ongunstig.
    Each value is None when no signal was found.

    - pdf_paths is sorted lexically (determinism contract) before processing.
    - First non-null hit per key wins; remaining PDFs are short-circuited per key.
    - Empty pdf_paths returns all-None result — never raises.
    - Emits structlog.info once per call with extraction stats.

    lang: "fr" for French patterns, "nl" (or anything else) for NL-Brussels patterns.
    """
    if not pdf_paths:
        return dict(_EMPTY_RESULT)

    sorted_paths = sorted(pdf_paths)

    floors_re = _FLOORS_FR_RE if lang == "fr" else _FLOORS_NL_RE
    units_re = _UNITS_FR_RE if lang == "fr" else _UNITS_NL_RE
    iioa_re = _IIOA_FR_RE if lang == "fr" else _IIOA_NL_RE

    units: int | None = None
    floors: int | None = None
    iioa_class: int | None = None
    mentions_ongunstig: bool | None = None
    hits: dict[str, bool] = {}

    for pdf_path in sorted_paths:
        # Short-circuit once all keys are resolved
        if (
            units is not None
            and floors is not None
            and iioa_class is not None
            and mentions_ongunstig is not None
        ):
            break

        try:
            text = await extract_text(pdf_path)
        except Exception as exc:
            log.warning("pdf_features_extract_text_failed", path=str(pdf_path), error=str(exc))
            text = ""

        if not text:
            continue

        if floors is None:
            m = floors_re.search(text)
            if m:
                try:
                    floors = int(m.group(1))
                    hits["floors"] = True
                except (ValueError, TypeError):
                    pass

        if units is None:
            m = units_re.search(text)
            if m:
                try:
                    units = int(m.group(1))
                    hits["units"] = True
                except (ValueError, TypeError):
                    pass

        if iioa_class is None:
            m = iioa_re.search(text)
            if m:
                raw = m.group(1).upper()
                resolved = _ROMAN_TO_INT.get(raw) or (int(raw) if raw.isdigit() else None)
                if resolved is not None:
                    iioa_class = resolved
                    hits["iioa_class"] = True

        if mentions_ongunstig is None:
            if lang == "fr":
                if _ONGUNSTIG_FR_RE.search(text):
                    mentions_ongunstig = True
                    hits["mentions_ongunstig"] = True
            else:
                if _ONGUNSTIG_NL_BR_RE.search(text):
                    mentions_ongunstig = True
                    hits["mentions_ongunstig"] = True

    log.info(
        "pdf_features_extracted",
        count=len(sorted_paths),
        hits=list(hits.keys()),
    )

    return {
        "units": units,
        "floors": floors,
        "iioa_class": iioa_class,
        "mentions_ongunstig": mentions_ongunstig,
    }
