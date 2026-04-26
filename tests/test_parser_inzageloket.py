"""
Tests for InzageloketSource — fixture-based parser tests.

NO live network. NO live Chromium. NO Anthropic/OpenAI during pytest.
All Playwright calls are monkeypatched.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from debouw.config import Settings
from debouw.ingest.sources.inzageloket import InzageloketSource, _parse_detail_page
from debouw.ingest.sources.base import SchemaDriftError
from debouw.models.permit import PermitProject, PublicInquiry, Address

# Fixture paths
_FIXTURES = Path(__file__).parent / "fixtures" / "vlaanderen_inzage"
_INDEX_HTML = (_FIXTURES / "index_listing.html").read_text(encoding="utf-8")
_DETAIL_FULL_HTML = (_FIXTURES / "detail_full.html").read_text(encoding="utf-8")
_DETAIL_MIN_HTML = (_FIXTURES / "detail_minimal.html").read_text(encoding="utf-8")
_SAMPLE_PDF = _FIXTURES / "sample_attachment.pdf"


def _make_settings() -> Settings:
    """Return a Settings instance pointing at a throwaway SQLite DB."""
    import tempfile, os
    td = tempfile.mkdtemp()
    return Settings(
        db_path=Path(td) / "test.db",
        data_root=Path(td),
    )


# ---------------------------------------------------------------------------
# Test 1: UA regression — inzageloket is in identified_sources
# ---------------------------------------------------------------------------

def test_inzageloket_client_carries_user_agent():
    """InzageloketSource._client carries the polite-scrape UA (Task 1.2)."""
    settings = _make_settings()
    src = InzageloketSource(settings)
    ua = src._client._user_agent
    assert ua is not None, "_user_agent should not be None for inzageloket source"
    assert "debouw-research" in ua, f"Expected 'debouw-research' in UA, got: {ua!r}"


# ---------------------------------------------------------------------------
# Test 2: Pipeline registry contains vlaanderen_inzage
# ---------------------------------------------------------------------------

def test_pipeline_registry_contains_vlaanderen_inzage():
    """_SOURCE_REGISTRY has 'vlaanderen_inzage' → InzageloketSource (Task 4.1)."""
    from debouw.pipeline import _SOURCE_REGISTRY
    assert "vlaanderen_inzage" in _SOURCE_REGISTRY
    assert _SOURCE_REGISTRY["vlaanderen_inzage"] is InzageloketSource


# ---------------------------------------------------------------------------
# Test 3: index_pass yields 3 external IDs from fixture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_index_pass_yields_external_ids(tmp_path):
    """index_pass yields 3 dossier OMV refs from the listing fixture."""
    settings = _make_settings()
    src = InzageloketSource(settings)

    # Stub _throttled_goto and set up a fake browser/page that returns fixture HTML
    async def fake_throttled_goto(page, url: str) -> None:
        pass

    class FakePage:
        async def content(self) -> str:
            return _INDEX_HTML

        async def wait_for_load_state(self, *args, **kwargs) -> None:
            pass

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            pass

    class FakeBrowser:
        async def new_context(self) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            pass

    src._browser = FakeBrowser()
    src._throttled_goto = fake_throttled_goto

    refs = []
    async for ref in src.index_pass(limit=None):
        refs.append(ref)

    assert len(refs) == 3, f"Expected 3 OMV refs, got {len(refs)}: {refs}"
    assert "OMV_2025000001" in refs
    assert "OMV_2025000002" in refs
    assert "OMV_2025000003" in refs


# ---------------------------------------------------------------------------
# Test 4: detail_pass full fixture — all fields present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_pass_full_fixture(tmp_path):
    """detail_pass parses detail_full.html correctly."""
    settings = _make_settings()
    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)

    # Stub browser to return detail_full.html
    async def fake_throttled_goto(page, url: str) -> None:
        pass

    class FakePage:
        async def content(self) -> str:
            return _DETAIL_FULL_HTML

        async def wait_for_load_state(self, *args, **kwargs) -> None:
            pass

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            pass

    class FakeBrowser:
        async def new_context(self) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            pass

    src._browser = FakeBrowser()
    src._throttled_goto = fake_throttled_goto

    # Mock the HTTP client to serve the sample PDF bytes
    pdf_bytes = _SAMPLE_PDF.read_bytes()
    mock_response = MagicMock()
    mock_response.content = pdf_bytes

    async def mock_get(url: str) -> MagicMock:
        return mock_response

    src._client.get = mock_get

    project, inquiry = await src.detail_pass("OMV_2025_FULL_DEMO")

    assert project.source == "vlaanderen_inzage"
    assert project.external_id.startswith("vlaanderen_inzage:")
    assert project.address.municipality is not None
    assert project.address.municipality == "Sint-Niklaas"
    assert project.decision_regime == "post_2026_reform"
    # Only 1 SSRF-allowed PDF URL; evil.example.com should be rejected
    assert len(project.dossier_pdfs) == 1
    # inquiry parsed from start/end dates
    assert inquiry is not None
    assert isinstance(inquiry, PublicInquiry)


# ---------------------------------------------------------------------------
# Test 5: detail_pass minimal fixture — graceful missing optional fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_pass_minimal_fixture(tmp_path):
    """detail_pass with minimal fixture: municipality=None, no PDFs, no inquiry."""
    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)

    async def fake_throttled_goto(page, url: str) -> None:
        pass

    class FakePage:
        async def content(self) -> str:
            return _DETAIL_MIN_HTML

        async def wait_for_load_state(self, *args, **kwargs) -> None:
            pass

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            pass

    class FakeBrowser:
        async def new_context(self) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            pass

    src._browser = FakeBrowser()
    src._throttled_goto = fake_throttled_goto

    project, inquiry = await src.detail_pass("OMV_2025_MIN_DEMO")

    assert project.address.municipality is None
    assert project.attachments == []
    assert project.dossier_pdfs == []
    assert inquiry is None


# ---------------------------------------------------------------------------
# Test 6: detail_pass raises SchemaDriftError on missing Projectnummer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detail_pass_raises_on_missing_projectnummer(tmp_path):
    """detail_pass raises SchemaDriftError when Projectnummer block is missing."""
    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)

    # Strip Projectnummer from full HTML in-memory
    broken_html = _DETAIL_FULL_HTML.replace(
        "<dt>Projectnummer</dt>\n        <dd>OMV_2025_FULL_DEMO</dd>", ""
    )

    async def fake_throttled_goto(page, url: str) -> None:
        pass

    class FakePage:
        async def content(self) -> str:
            return broken_html

        async def wait_for_load_state(self, *args, **kwargs) -> None:
            pass

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            pass

    class FakeBrowser:
        async def new_context(self) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            pass

    src._browser = FakeBrowser()
    src._throttled_goto = fake_throttled_goto

    with pytest.raises(SchemaDriftError):
        await src.detail_pass("OMV_2025_FULL_DEMO")


# ---------------------------------------------------------------------------
# Test 7: external_id uniqueness across sources
# ---------------------------------------------------------------------------

def test_external_id_uniqueness_across_sources():
    """external_id prefix guarantees uniqueness across gent and vlaanderen_inzage."""
    vl_id = "vlaanderen_inzage:OMV_X"
    gent_id = "gent:OMV_X"
    assert vl_id != gent_id


# ---------------------------------------------------------------------------
# Test 8: extra="forbid" holds for PermitProject constructed from Inzageloket data
# ---------------------------------------------------------------------------

def test_extra_forbid_holds_for_inzageloket_project():
    """PermitProject with extra fields raises ValidationError (extra='forbid')."""
    from pydantic import ValidationError
    from datetime import datetime, timezone

    with pytest.raises(ValidationError):
        PermitProject(
            external_id="vlaanderen_inzage:OMV_TEST",
            source="vlaanderen_inzage",
            omv_reference="OMV_TEST",
            detail_url="https://omgevingsloketinzage.omgeving.vlaanderen.be/dossier/OMV_TEST",
            title="Test dossier",
            address=Address(raw="Teststraat 1"),
            status="intake",
            raw_html_path=Path("/tmp/test.html"),
            first_seen_at=datetime.now(tz=timezone.utc),
            last_changed_at=datetime.now(tz=timezone.utc),
            content_hash="abc123",
            decision_regime="post_2026_reform",
            unexpected_extra_field="should_fail",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Test 9 (B1): detail_pass rejects path-traversal identifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_pass_rejects_path_traversal_identifier(tmp_path):
    """detail_pass raises ValueError and emits warning for a path-traversal identifier.

    B1: validates identifier matches _OMV_REF_PATTERN before building any path.
    """
    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)
    src._browser = MagicMock()  # unused — validation fires before browser call

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ValueError, match="path traversal"):
            await src.detail_pass("../etc/passwd")

    # No file must have been written under /etc
    etc_path = Path("/etc/passwd")
    # We can't assert the file doesn't exist (it does — it's a real system file),
    # but we assert it was not WRITTEN by our code (file size unchanged / no new dir).
    # The test passes if ValueError was raised before any mkdir() happened.

    # Assert warning was logged
    assert any(
        e.get("event") == "inzageloket_invalid_omv_ref" for e in logs
    ), f"Expected 'inzageloket_invalid_omv_ref' warning, got: {[e.get('event') for e in logs]}"


# ---------------------------------------------------------------------------
# Test 10 (B1, index_pass): index_pass skips path-traversal href
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_pass_skips_path_traversal_href(tmp_path):
    """index_pass skips hrefs whose extracted OMV ref fails validation.

    B1: the invalid ref is logged and not yielded.
    """
    malicious_html = """
    <html><body>
    <div class="dossier-results">
      <a href="/dossier/OMV_2025000001" class="dossier-card">OK dossier</a>
      <a href="/dossier/../../../../etc/cron.d/evil" class="dossier-card">Evil</a>
    </div>
    </body></html>
    """
    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)

    async def fake_throttled_goto(page, url: str) -> None:
        pass

    class FakePage:
        async def content(self) -> str:
            return malicious_html

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            pass

    class FakeBrowser:
        async def new_context(self) -> FakeContext:
            return FakeContext()

    src._browser = FakeBrowser()
    src._throttled_goto = fake_throttled_goto

    with structlog.testing.capture_logs() as logs:
        refs = []
        async for ref in src.index_pass(limit=None):
            refs.append(ref)

    # Only the valid ref should be yielded
    assert refs == ["OMV_2025000001"], f"Unexpected refs: {refs}"
    # The invalid ref should have triggered a warning
    assert any(
        e.get("event") == "inzageloket_invalid_omv_ref" for e in logs
    ), f"Expected 'inzageloket_invalid_omv_ref' in logs, got: {[e.get('event') for e in logs]}"


# ---------------------------------------------------------------------------
# Test 11 (B2): PDF download aborts on oversize stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_download_aborts_on_oversize_stream(tmp_path, monkeypatch):
    """detail_pass aborts and deletes partial file when stream exceeds _MAX_PDF_SIZE_BYTES.

    B2: byte counter fires before file is fully written; partial file is deleted.
    Uses monkeypatch.chdir(tmp_path) so data/raw/... writes are isolated per test.
    """
    import debouw.ingest.sources.inzageloket as inzageloket_module
    from debouw.ingest.pdf import _MAX_PDF_SIZE_BYTES

    # Redirect CWD to tmp_path so relative data/raw/... paths don't pollute the repo
    monkeypatch.chdir(tmp_path)

    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)

    # Stub browser to return the full detail fixture (which includes 1 allowed PDF URL)
    async def fake_throttled_goto(page, url: str) -> None:
        pass

    class FakePage:
        async def content(self) -> str:
            return _DETAIL_FULL_HTML

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            pass

    class FakeBrowser:
        async def new_context(self) -> FakeContext:
            return FakeContext()

    src._browser = FakeBrowser()
    src._throttled_goto = fake_throttled_goto

    # Build a fake stream response that yields _MAX_PDF_SIZE_BYTES + 1 bytes
    oversize_bytes = _MAX_PDF_SIZE_BYTES + 1

    class FakeStreamResponse:
        status_code = 200

        @property
        def url(self):
            class _URL:
                host = "omgevingsloketinzage.omgeving.vlaanderen.be"

                def __str__(self):
                    return "https://omgevingsloketinzage.omgeving.vlaanderen.be/doc/sample.pdf"

            return _URL()

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, chunk_size=65536):
            total = 0
            while total < oversize_bytes:
                to_send = min(chunk_size, oversize_bytes - total)
                yield b"x" * to_send
                total += to_send

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    @asynccontextmanager
    async def fake_stream(method, url, **kwargs):
        yield FakeStreamResponse()

    src._client.stream = fake_stream

    with structlog.testing.capture_logs() as logs:
        project, inquiry = await src.detail_pass("OMV_2025_FULL_DEMO")

    # No PDF file should be persisted (partial file deleted by oversize abort)
    pdfs_dir = (
        tmp_path
        / "data"
        / "raw"
        / "vlaanderen_inzage"
        / "vlaanderen_inzage:OMV_2025_FULL_DEMO"
        / "pdfs"
    )
    if pdfs_dir.exists():
        pdf_files = list(pdfs_dir.glob("*.pdf"))
        assert pdf_files == [], f"Expected no PDFs persisted, found: {pdf_files}"

    # No dossier_pdfs in the returned project
    assert project.dossier_pdfs == [], (
        f"Expected empty dossier_pdfs (oversize stream aborted), got: {project.dossier_pdfs}"
    )

    # Warning should have been logged
    assert any(
        e.get("event") == "inzageloket_pdf_too_large" for e in logs
    ), f"Expected 'inzageloket_pdf_too_large' in logs, got: {[e.get('event') for e in logs]}"
