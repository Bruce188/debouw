"""Schema-drift lock tests for InzageloketSource.

Locks the selector and label-literal contract against synthetic fixtures.
NO live network. NO live Chromium.
If either test breaks, update plan-v8 Phase 2 selectors before patching.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from debouw.config import Settings
from debouw.ingest.sources.base import SchemaDriftError
from debouw.ingest.sources.inzageloket import InzageloketSource

_FIXTURES = Path(__file__).parent / "fixtures" / "vlaanderen_inzage"
_INDEX_HTML = (_FIXTURES / "index_listing.html").read_text(encoding="utf-8")
_DETAIL_FULL_HTML = (_FIXTURES / "detail_full.html").read_text(encoding="utf-8")


def _make_settings() -> Settings:
    td = tempfile.mkdtemp()
    return Settings(
        db_path=Path(td) / "test.db",
        data_root=Path(td),
    )


class FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    async def content(self) -> str:
        return self._html

    async def wait_for_load_state(self, *args, **kwargs) -> None:  # noqa: ARG002
        pass


class FakeContext:
    def __init__(self, html: str) -> None:
        self._html = html

    async def new_page(self) -> FakePage:
        return FakePage(self._html)

    async def close(self) -> None:
        pass


class FakeBrowser:
    def __init__(self, html: str) -> None:
        self._html = html

    async def new_context(self) -> FakeContext:
        return FakeContext(self._html)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_schema_drift_listing_container_renamed_raises_drift():
    """index_pass raises SchemaDriftError when listing container class is renamed.

    Locks _LISTING_CONTAINER_CLASS = "dossier-results" against synthetic fixture.
    If this test fails after a probe run, update plan-v8 Task 2.2.
    """
    # Mutate the container class — dossier-results → dossier-grid
    mutated_html = re.sub(r"dossier-results", "dossier-grid", _INDEX_HTML)
    assert "dossier-results" not in mutated_html, "Mutation did not take effect"

    settings = _make_settings()
    src = InzageloketSource(settings)
    src._browser = FakeBrowser(mutated_html)  # type: ignore[assignment]

    async def fake_throttled_goto(page, url: str) -> None:  # noqa: ARG001
        pass

    src._throttled_goto = fake_throttled_goto

    with pytest.raises(SchemaDriftError):
        refs = []
        async for ref in src.index_pass(limit=None):
            refs.append(ref)


@pytest.mark.asyncio
async def test_schema_drift_detail_field_label_renamed_raises_drift(tmp_path):
    """detail_pass raises SchemaDriftError when Projectnummer label is renamed.

    Locks the <dt>Projectnummer</dt> label against synthetic fixture.
    If this test fails after a probe run, update plan-v8 Task 2.3.
    """
    # Mutate the required label — Projectnummer → Projektnummer (Dutch typo / rename)
    mutated_html = _DETAIL_FULL_HTML.replace("Projectnummer", "Projektnummer")
    assert "Projectnummer" not in mutated_html, "Mutation did not take effect"

    settings = Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path,
    )
    src = InzageloketSource(settings)
    src._browser = FakeBrowser(mutated_html)  # type: ignore[assignment]

    async def fake_throttled_goto(page, url: str) -> None:  # noqa: ARG001
        pass

    src._throttled_goto = fake_throttled_goto

    with pytest.raises(SchemaDriftError):
        await src.detail_pass("OMV_2025_FULL_DEMO")
