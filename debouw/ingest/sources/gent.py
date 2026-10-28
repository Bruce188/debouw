"""
Gent tenant of the Remmicom "Consultatie Module Burger" platform.

The Gent-specific scraper was generalized into the multi-tenant connector in
debouw.ingest.sources.consultatie (~110 Flemish municipalities run the same
platform at <slug>.consultatieomgeving.net). This module re-exports the
Gent-pinned source plus the shared parsing helpers so existing imports
(pipeline, CLI, tests) keep working unchanged.

Gent keeps its historical contract: bare-uuid identifiers, external ids
'gent:OMV_…', source label 'gent_consultatie', and raw HTML under
data/raw/gent_consultatie/<uuid>/.
"""

from __future__ import annotations

from debouw.ingest.sources.consultatie import (  # noqa: F401
    _HOUSE_NUMBER_RE,
    _NL_MONTHS,
    _PARCEL_RE,
    _PERIODE_RE,
    _POSTCODE_RE,
    GentSource,
    _parse_date_attr,
    _parse_nl_date,
    address_geocode_query,
    parse_gent_address,
)

__all__ = [
    "GentSource",
    "address_geocode_query",
    "parse_gent_address",
]
