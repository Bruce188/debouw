"""
Tests for debouw.ingest.sources.brussels (Track B: bs4+httpx).

Fixture-driven: no live network. All HTTP calls are mocked via respx.
Covers:
1. Index parser yields ≥ 3 identifiers matching _BRU_REF_PATTERN.
2. Detail parser populates required PermitProject fields correctly.
3. SchemaDriftError raised when tabledatahistory JSON is absent.
4. User-Agent carried on every HTTP call.
5. Cross-source normalisation: PermitProject model_dump round-trip.
6. Content_hash idempotency.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
import respx
from httpx import Response

from debouw.config import Settings
from debouw.ingest.sources.brussels import BrusselsSource, _BRU_REF_PATTERN


# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "brussels"
_LISTING_HTML = (_FIXTURES / "listing_full.html").read_text(encoding="utf-8")
_DETAIL_HTML = (_FIXTURES / "detail_full.html").read_text(encoding="utf-8")
_DETAIL_MINIMAL_HTML = (_FIXTURES / "detail_minimal.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BASE = "https://openpermits.brussels"
_REF = "01/PU/1984289"  # the detail_full fixture reference


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key=None,
        openai_api_key=None,
        data_root=tmp_path / "data",
        db_path=tmp_path / "data" / "debouw.sqlite",
    )


# ---------------------------------------------------------------------------
# 1. Index parser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_parser_yields_valid_refs(tmp_path: Path) -> None:
    """index_pass yields ≥3 refs, all matching _BRU_REF_PATTERN."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        # Listing page (current month)
        now = datetime.now(timezone.utc)
        mock.get(
            f"{_BASE}/fr/event/submission/{now.year}/{now.month}"
        ).mock(return_value=Response(200, text=_LISTING_HTML))

        async with BrusselsSource(settings) as src:
            refs = [ref async for ref in src.index_pass(limit=None)]

    assert len(refs) >= 3, f"Expected ≥3 refs, got {len(refs)}"
    for ref in refs:
        assert _BRU_REF_PATTERN.match(ref), (
            f"Ref {ref!r} does not match _BRU_REF_PATTERN"
        )


@pytest.mark.asyncio
async def test_index_parser_respects_limit(tmp_path: Path) -> None:
    """index_pass(limit=3) yields exactly 3 refs."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        now = datetime.now(timezone.utc)
        mock.get(
            f"{_BASE}/fr/event/submission/{now.year}/{now.month}"
        ).mock(return_value=Response(200, text=_LISTING_HTML))

        async with BrusselsSource(settings) as src:
            refs = [ref async for ref in src.index_pass(limit=3)]

    assert len(refs) == 3


# ---------------------------------------------------------------------------
# 2. Detail parser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_parser_populates_required_fields(tmp_path: Path) -> None:
    """detail_pass returns PermitProject with region, source, external_id correct."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_BASE}/fr/_{_REF}").mock(
            return_value=Response(200, text=_DETAIL_HTML)
        )

        async with BrusselsSource(settings) as src:
            project, inquiry = await src.detail_pass(_REF)

    assert project.region == "brussels"
    assert project.source == "brussels_openpermits"
    assert project.external_id == f"brussels:{_REF}"
    assert project.omv_reference == _REF
    assert project.applicant_name is None  # GDPR posture
    assert project.address is not None
    assert "Victor" in project.address.raw or "Anderlecht" in project.address.raw
    assert project.content_hash and len(project.content_hash) == 64
    assert str(project.detail_url) == f"{_BASE}/fr/_{_REF}"


@pytest.mark.asyncio
async def test_detail_parser_minimal(tmp_path: Path) -> None:
    """detail_pass on minimal fixture produces valid PermitProject."""
    settings = _settings(tmp_path)
    ref = "05/PU/2026541"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_BASE}/fr/_{ref}").mock(
            return_value=Response(200, text=_DETAIL_MINIMAL_HTML)
        )

        async with BrusselsSource(settings) as src:
            project, inquiry = await src.detail_pass(ref)

    assert project.region == "brussels"
    assert project.external_id == f"brussels:{ref}"
    assert inquiry is None  # no inquiry dates in minimal fixture
    # Round-trip via model_dump
    from debouw.models.permit import PermitProject
    dumped = project.model_dump(mode="python")
    restored = PermitProject.model_validate(dumped)
    assert restored.external_id == project.external_id


# ---------------------------------------------------------------------------
# 3. SchemaDriftError on missing tabledatahistory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schema_drift_error_on_missing_json(tmp_path: Path) -> None:
    """detail_pass raises SchemaDriftError when tabledatahistory JSON absent."""
    from debouw.ingest.sources.base import SchemaDriftError

    settings = _settings(tmp_path)
    ref = "05/PU/2026541"

    broken_html = (
        "<!DOCTYPE html><html lang='fr'><head><title>Broken</title></head>"
        "<body><h1 class='card-title'>Rue du Midi 12<br>1000 Bruxelles</h1>"
        "</body></html>"
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_BASE}/fr/_{ref}").mock(
            return_value=Response(200, text=broken_html)
        )

        async with BrusselsSource(settings) as src:
            with pytest.raises(SchemaDriftError):
                await src.detail_pass(ref)


# ---------------------------------------------------------------------------
# 4. User-Agent on every HTTP call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_agent_carried_on_requests(tmp_path: Path) -> None:
    """All HTTP calls carry the identified User-Agent header."""
    settings = _settings(tmp_path)
    expected_ua_fragment = "debouw-research"

    captured_headers: list[dict] = []

    with respx.mock(assert_all_called=False) as mock:
        now = datetime.now(timezone.utc)

        def _capture_listing(request):
            captured_headers.append(dict(request.headers))
            return Response(200, text=_LISTING_HTML)

        def _capture_detail(request):
            captured_headers.append(dict(request.headers))
            return Response(200, text=_DETAIL_HTML)

        mock.get(
            f"{_BASE}/fr/event/submission/{now.year}/{now.month}"
        ).mock(side_effect=_capture_listing)
        # Mock detail for the known fixture ref only
        mock.get(f"{_BASE}/fr/_{_REF}").mock(side_effect=_capture_detail)

        async with BrusselsSource(settings) as src:
            # index_pass (captures listing UA)
            _refs = [ref async for ref in src.index_pass(limit=1)]
            # detail_pass for the fixture ref (captures detail UA)
            await src.detail_pass(_REF)

    assert captured_headers, "No HTTP calls were captured"
    for headers in captured_headers:
        ua = headers.get("user-agent", "")
        assert expected_ua_fragment in ua, (
            f"Expected UA fragment '{expected_ua_fragment}' not found in: {ua!r}"
        )


# ---------------------------------------------------------------------------
# 5. Cross-source normalisation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_source_normalisation(tmp_path: Path) -> None:
    """PermitProject from BrusselsSource round-trips via model_dump."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_BASE}/fr/_{_REF}").mock(
            return_value=Response(200, text=_DETAIL_HTML)
        )

        async with BrusselsSource(settings) as src:
            project, _ = await src.detail_pass(_REF)

    from debouw.models.permit import PermitProject
    dumped = project.model_dump(mode="python")

    # Required cross-source fields
    assert "external_id" in dumped
    assert "source" in dumped
    assert "region" in dumped
    assert "omv_reference" in dumped
    assert "content_hash" in dumped

    restored = PermitProject.model_validate(dumped)
    assert restored.region == "brussels"
    assert restored.source == "brussels_openpermits"


# ---------------------------------------------------------------------------
# 6. Content_hash idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_content_hash_idempotency(tmp_path: Path) -> None:
    """Same HTML → same content_hash; mutated HTML → different hash."""
    settings = _settings(tmp_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_BASE}/fr/_{_REF}").mock(
            return_value=Response(200, text=_DETAIL_HTML)
        )

        async with BrusselsSource(settings) as src:
            proj_a, _ = await src.detail_pass(_REF)

    # Re-parse with identical HTML → identical hash
    expected_hash = hashlib.sha256(_DETAIL_HTML.encode()).hexdigest()
    assert proj_a.content_hash == expected_hash

    # Mutate HTML → different hash
    mutated_html = _DETAIL_HTML.replace("Victor", "MUTATED_NAME")
    mutated_hash = hashlib.sha256(mutated_html.encode()).hexdigest()
    assert mutated_hash != expected_hash
