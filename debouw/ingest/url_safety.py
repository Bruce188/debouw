"""
URL safety helpers for scraped attachment URLs.

Cross-feature intel #3 (security-auditor, plan-v4 review): scraped
attachment URLs MUST be allow-listed before being passed to a download
helper. The Inzageloket scraper hosts attachments on the Vlaanderen
domain; expansion (regional .gov.be subdomains) is documented in code.
"""

from __future__ import annotations

from urllib.parse import urlparse

import structlog

log = structlog.get_logger(__name__)

# Initial allowlist — host-equality ONLY (no wildcards, no suffix-match).
_INZAGELOKET_ATTACHMENT_HOSTS: frozenset[str] = frozenset({
    "omgevingsloketinzage.omgeving.vlaanderen.be",
})


def is_inzageloket_attachment_allowed(url: str) -> bool:
    """Return True if `url` is an allowed attachment URL for Inzageloket.

    Checks:
    - Scheme ∈ {"https", "http"}.
    - netloc ∈ _INZAGELOKET_ATTACHMENT_HOSTS (host equality, case-insensitive).

    On reject: log structured warning ("inzageloket_attachment_rejected")
    and return False.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        log.warning("inzageloket_url_parse_failed", url=url, error=str(exc))
        return False

    if parsed.scheme not in ("https", "http"):
        log.warning(
            "inzageloket_attachment_rejected",
            url=url,
            reason="bad_scheme",
            scheme=parsed.scheme,
        )
        return False

    netloc = parsed.netloc.lower()
    if netloc not in _INZAGELOKET_ATTACHMENT_HOSTS:
        log.warning(
            "inzageloket_attachment_rejected",
            url=url,
            reason="host_not_allowed",
            host=netloc,
        )
        return False

    return True
