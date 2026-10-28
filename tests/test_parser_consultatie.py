"""
Multi-tenant Remmicom "Consultatie Module Burger" connector tests.

Fixtures under tests/fixtures/vlaanderen_consultatie/ are REAL captured HTML
(2026-06-12) from three tenants exhibiting the three structural forms:
  - antwerpen: Word-table detail (<td>Label:</td><td>value</td>)
  - hasselt:   free prose ("Omgevingsloket nummer : OMV_…", "gelegen te …")
  - aalter:    prose variant ("Omgevingsvergunning: OMV_…", "gekend in het
               kadaster als …"), period stated ONLY on the index card
Personal names / applicant contact addresses were sanitized at capture time.

All HTTP is mocked via respx. No live network calls.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import respx
from bs4 import BeautifulSoup
from httpx import Response

from debouw.config import Settings
from debouw.ingest.sources.base import DossierVanishedError, SchemaDriftError
from debouw.ingest.sources.consultatie import (
    ConsultatieSource,
    GentSource,
    LABEL_SYNONYMS,
    _parse_date_attr,
    discover_tenants,
    find_labelled_value,
    load_tenants,
    municipality_slug,
    parse_detail_fields,
    slug_candidates,
    tenants_file_path,
)

FIXTURES = Path(__file__).parent / "fixtures" / "vlaanderen_consultatie"
GENT_FIXTURES = Path(__file__).parent / "fixtures"

ANTWERPEN_INDEX = (FIXTURES / "antwerpen_index.html").read_text(encoding="utf-8")
ANTWERPEN_DETAIL = (FIXTURES / "antwerpen_detail1.html").read_text(encoding="utf-8")
HASSELT_INDEX = (FIXTURES / "hasselt_index.html").read_text(encoding="utf-8")
HASSELT_DETAIL = (FIXTURES / "hasselt_detail1.html").read_text(encoding="utf-8")
AALTER_INDEX = (FIXTURES / "aalter_index.html").read_text(encoding="utf-8")
AALTER_DETAIL = (FIXTURES / "aalter_detail1.html").read_text(encoding="utf-8")
GENT_DETAIL = (GENT_FIXTURES / "gent_detail_with_address.html").read_text(
    encoding="utf-8"
)
GENT_INDEX = (GENT_FIXTURES / "gent_index.html").read_text(encoding="utf-8")

# First-card UUIDs in the (truncated) index fixtures
ANTWERPEN_UUID = "3eda341f-2962-40c3-a0c3-01b2e4e8198c"
AALTER_UUID = "955ae273-8653-4c77-9f31-5cc58a10ae42"


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        data_root=tmp_path,
        db_path=tmp_path / "test.sqlite",
        consultatie_tenants_file=tmp_path / "consultatie_tenants.json",
        throttle_vlaanderen_consultatie_seconds=0.001,
        throttle_gent_seconds=0.001,
        throttle_basisregisters_seconds=0.001,
        **overrides,
    )


def _site(slug: str) -> str:
    return f"https://{slug}.consultatieomgeving.net"


def _mock_tenant(
    mock: respx.MockRouter,
    slug: str,
    index_html: str,
    detail_html: str,
    *,
    robots: Response | None = None,
) -> None:
    mock.get(f"{_site(slug)}/robots.txt").mock(
        return_value=robots or Response(404)
    )
    mock.get(f"{_site(slug)}/burger/nl/OpenbareOnderzoeken").mock(
        return_value=Response(200, text=index_html)
    )
    mock.get(url__regex=rf"https://{slug}\.consultatieomgeving\.net/burger/nl/OpenbareOnderzoeken/Details/.*").mock(
        return_value=Response(200, text=detail_html)
    )


# ---------------------------------------------------------------------------
# Slug rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Gent", "gent"),
        ("Antwerpen", "antwerpen"),
        ("Heist-op-den-Berg", "heist-op-den-berg"),  # hyphens preserved (live-verified)
        ("Sint-Pieters-Leeuw", "sint-pieters-leeuw"),
        ("De Panne", "de-panne"),  # spaces -> hyphens
        ("Évergem", "evergem"),  # diacritics stripped
        ("'s-Hertogenbosch", "s-hertogenbosch"),  # apostrophes dropped
    ],
)
def test_municipality_slug(name: str, expected: str) -> None:
    assert municipality_slug(name) == expected


def test_slug_candidates_hyphenated() -> None:
    assert slug_candidates("Heist-op-den-Berg") == [
        "heist-op-den-berg",
        "heistopdenberg",
    ]


def test_slug_candidates_simple() -> None:
    assert slug_candidates("Gent") == ["gent"]


# ---------------------------------------------------------------------------
# Tolerant label matrix — three structural forms, real captured pages
# ---------------------------------------------------------------------------


def test_parse_detail_fields_antwerpen_word_table() -> None:
    """Antwerpen uses a Word table with drifted labels (Projectnummer /
    Voorwerp van de aanvraag / Vergunningsplichten / Ligging van het project)."""
    soup = BeautifulSoup(ANTWERPEN_DETAIL, "html.parser")
    fields = parse_detail_fields(soup, source_key="vlaanderen_consultatie", url="t")
    assert fields["omv_reference"] == "OMV_2025134402"
    assert fields["project_type"] == "Stedenbouwkundige handelingen"
    assert (
        fields["locatie_text"]
        == "Lange Nieuwstraat 64-68, Eikenstraat 2 te 2000 Antwerpen"
    )
    assert fields["parcel_text"] == "afdeling 3 sectie C nr. 2477A"
    assert fields["title"] is not None
    assert fields["title"].startswith("verbouwen en uitbreiden van het gebouw")


def test_parse_detail_fields_hasselt_prose() -> None:
    """Hasselt is free prose: 'Omgevingsloket nummer : OMV_…', 'gelegen te …,
    kadastraal gekend …'."""
    soup = BeautifulSoup(HASSELT_DETAIL, "html.parser")
    fields = parse_detail_fields(soup, source_key="vlaanderen_consultatie", url="t")
    assert fields["omv_reference"] == "OMV_2026031590"
    assert fields["locatie_text"] == "Bekstraat zn"
    assert fields["parcel_text"] == "afdeling 10 sectie C nr. 632D"
    assert fields["title"] is not None
    assert fields["title"].startswith("verkavelen van een perceel in 2 loten")
    assert fields["project_type"] is not None
    assert "verkavelen" in fields["project_type"]


def test_parse_detail_fields_aalter_prose_variant() -> None:
    """Aalter prose variant: 'Omgevingsvergunning: OMV_…', '… en gekend in het
    kadaster als …' (no 'gelegen te')."""
    soup = BeautifulSoup(AALTER_DETAIL, "html.parser")
    fields = parse_detail_fields(soup, source_key="vlaanderen_consultatie", url="t")
    assert fields["omv_reference"] == "OMV_2025158119"
    assert fields["parcel_text"] == "afdeling 1 sectie A nrs. 287V en 293B"
    assert "Weststraat 29" in fields["locatie_text"]
    assert fields["project_type"] is not None
    assert fields["project_type"].startswith("verkavelen van percelen")


def test_find_labelled_value_dt_dd_form() -> None:
    """Gent legacy dt/dd form still resolves through the synonym lists."""
    soup = BeautifulSoup(GENT_DETAIL, "html.parser")
    assert find_labelled_value(soup, LABEL_SYNONYMS["omv_reference"]) is not None
    assert find_labelled_value(soup, LABEL_SYNONYMS["location"]) is not None


def test_parse_detail_fields_unknown_labels_raises_schema_drift() -> None:
    """A page where NO synonym matches and no OMV_ pattern exists must raise."""
    html = """
    <html><body><section id="x">
    <table>
      <tr><td>Referentie:</td><td>REF-123</td></tr>
      <tr><td>Beschrijving:</td><td>nieuwbouw</td></tr>
    </table>
    </section></body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    with pytest.raises(SchemaDriftError) as excinfo:
        parse_detail_fields(soup, source_key="vlaanderen_consultatie", url="http://t")
    assert "vlaanderen_consultatie" in str(excinfo.value)


def test_parse_detail_fields_synonym_miss_falls_back_to_omv_regex() -> None:
    """An unknown label but an OMV_ reference in prose must NOT raise."""
    html = "<html><body><p>Dossier OMV_2026000001 ligt ter inzage.</p></body></html>"
    soup = BeautifulSoup(html, "html.parser")
    fields = parse_detail_fields(soup, source_key="vlaanderen_consultatie", url="t")
    assert fields["omv_reference"] == "OMV_2026000001"


def test_parse_detail_fields_platform_404_raises_vanished() -> None:
    """Remmicom serves 'Pagina niet gevonden' with HTTP 200 when a dossier is
    withdrawn between index and detail pass — must classify as vanished, not
    as schema drift (real capture from antwerpen, 2026-06-13)."""
    html = (FIXTURES / "remmicom_404.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    with pytest.raises(DossierVanishedError) as excinfo:
        parse_detail_fields(
            soup,
            source_key="vlaanderen_consultatie",
            url="https://antwerpen.consultatieomgeving.net/burger/nl/OpenbareOnderzoeken/Details/x",
        )
    assert "vanished" in str(excinfo.value)
    assert not issubclass(DossierVanishedError, SchemaDriftError)


# ---------------------------------------------------------------------------
# Timestamp parsing — epoch seconds AND .NET ticks
# ---------------------------------------------------------------------------


def test_parse_date_attr_epoch_seconds() -> None:
    assert _parse_date_attr("1745280000") == date(2025, 4, 22)


def test_parse_date_attr_dotnet_ticks() -> None:
    # Real value from the antwerpen index capture (12 juni 2026)
    assert _parse_date_attr("639168192000000000") == date(2026, 6, 12)


def test_parse_date_attr_garbage() -> None:
    assert _parse_date_attr(None) is None
    assert _parse_date_attr("not-a-number") is None


# ---------------------------------------------------------------------------
# Multi-tenant source: index + detail (respx-mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_pass_yields_slug_qualified_ids(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_tenant(mock, "antwerpen", ANTWERPEN_INDEX, ANTWERPEN_DETAIL)
        _mock_tenant(mock, "hasselt", HASSELT_INDEX, HASSELT_DETAIL)
        async with ConsultatieSource(
            settings, tenants=["antwerpen", "hasselt"]
        ) as src:
            ids = [i async for i in src.index_pass(limit=None)]

    assert len(ids) == 6  # 3 cards per truncated index fixture
    assert sum(1 for i in ids if i.startswith("antwerpen/")) == 3
    assert sum(1 for i in ids if i.startswith("hasselt/")) == 3
    assert f"antwerpen/{ANTWERPEN_UUID}" in ids


@pytest.mark.asyncio
async def test_detail_pass_antwerpen_external_id_and_window(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_tenant(mock, "antwerpen", ANTWERPEN_INDEX, ANTWERPEN_DETAIL)
        async with ConsultatieSource(settings, tenants=["antwerpen"]) as src:
            project, inquiry = await src.detail_pass(f"antwerpen/{ANTWERPEN_UUID}")

    assert project.external_id == "antwerpen:OMV_2025134402"
    assert project.source == "vlaanderen_consultatie"
    assert project.region == "vl"
    assert project.applicant_name is None  # GDPR: never persisted
    # Address parsed from the Word-table 'Ligging van het project'
    assert project.address.street == "Lange Nieuwstraat"
    assert project.address.house_number == "64-68"
    assert project.address.postcode == "2000"
    assert project.address.municipality == "Antwerpen"
    assert project.address.parcel_id == "afdeling 3 sectie C nr. 2477A"
    # Inquiry window from the detail prose ('van 12 juni 2026 tot en met 11 juli 2026')
    assert inquiry is not None
    assert inquiry.external_id == f"antwerpen:{ANTWERPEN_UUID}"
    assert inquiry.period_start == date(2026, 6, 12)
    assert inquiry.period_end == date(2026, 7, 11)
    # Raw HTML lands in the per-tenant directory
    assert (
        tmp_path / "raw" / "vlaanderen_consultatie" / "antwerpen" / ANTWERPEN_UUID
    ).is_dir()


@pytest.mark.asyncio
async def test_aalter_window_from_index_card_cache(tmp_path: Path) -> None:
    """Aalter states the inquiry window ONLY on the index card (.NET ticks);
    the detail page has neither remOoDatum spans nor matching prose."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_tenant(mock, "aalter", AALTER_INDEX, AALTER_DETAIL)
        async with ConsultatieSource(settings, tenants=["aalter"]) as src:
            ids = [i async for i in src.index_pass(limit=None)]
            assert f"aalter/{AALTER_UUID}" in ids
            assert f"aalter/{AALTER_UUID}" in src._index_periods
            project, inquiry = await src.detail_pass(f"aalter/{AALTER_UUID}")

    assert project.external_id == "aalter:OMV_2025158119"
    assert inquiry is not None
    assert inquiry.period_start == date(2026, 6, 11)
    assert inquiry.period_end == date(2026, 7, 10)


@pytest.mark.asyncio
async def test_robots_disallow_skips_tenant(tmp_path: Path) -> None:
    """A tenant whose robots.txt gains a Disallow is skipped (and the index
    is never fetched); other tenants still process."""
    settings = _settings(tmp_path)
    robots = Response(200, text="User-agent: *\nDisallow: /\n")
    with respx.mock(assert_all_called=False) as mock:
        _mock_tenant(mock, "antwerpen", ANTWERPEN_INDEX, ANTWERPEN_DETAIL, robots=robots)
        _mock_tenant(mock, "hasselt", HASSELT_INDEX, HASSELT_DETAIL)
        antwerpen_index_route = mock.get(
            f"{_site('antwerpen')}/burger/nl/OpenbareOnderzoeken"
        )
        async with ConsultatieSource(
            settings, tenants=["antwerpen", "hasselt"]
        ) as src:
            ids = [i async for i in src.index_pass(limit=None)]

    assert all(i.startswith("hasselt/") for i in ids)
    assert len(ids) == 3
    assert not antwerpen_index_route.called


@pytest.mark.asyncio
async def test_index_pass_total_limit_across_tenants(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        _mock_tenant(mock, "antwerpen", ANTWERPEN_INDEX, ANTWERPEN_DETAIL)
        _mock_tenant(mock, "hasselt", HASSELT_INDEX, HASSELT_DETAIL)
        async with ConsultatieSource(
            settings, tenants=["antwerpen", "hasselt"]
        ) as src:
            ids = [i async for i in src.index_pass(limit=4)]

    assert len(ids) == 4
    assert sum(1 for i in ids if i.startswith("antwerpen/")) == 3
    assert sum(1 for i in ids if i.startswith("hasselt/")) == 1


# ---------------------------------------------------------------------------
# Gent backward compatibility — sweep and legacy source agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gent_external_id_stable_across_sources(tmp_path: Path) -> None:
    """Re-ingesting Gent through the multi-tenant sweep MUST hit the same
    external_ids, source label and inquiry ids as the legacy GentSource."""
    settings = _settings(tmp_path)
    uuid = "abc-123"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*gent\.consultatieomgeving.*Details/.*").mock(
            return_value=Response(200, text=GENT_DETAIL)
        )
        async with GentSource(settings) as legacy:
            legacy_project, legacy_inquiry = await legacy.detail_pass(uuid)
        async with ConsultatieSource(settings, tenants=["gent"]) as sweep:
            sweep_project, sweep_inquiry = await sweep.detail_pass(f"gent/{uuid}")

    assert legacy_project.external_id.startswith("gent:OMV_")
    assert sweep_project.external_id == legacy_project.external_id
    assert legacy_project.source == "gent_consultatie"
    assert sweep_project.source == "gent_consultatie"
    assert sweep_project.detail_url == legacy_project.detail_url
    if legacy_inquiry is not None:
        assert sweep_inquiry is not None
        assert sweep_inquiry.external_id == legacy_inquiry.external_id  # bare uuid
        assert sweep_inquiry.external_id == uuid
    # Same historical raw-HTML directory
    assert (tmp_path / "raw" / "gent_consultatie" / uuid / "detail.html").exists()


def test_vlaanderen_consultatie_in_pipeline_registry() -> None:
    from debouw.pipeline import _SOURCE_REGISTRY

    assert _SOURCE_REGISTRY["vlaanderen_consultatie"] is ConsultatieSource
    # gent stays registered and backward-compatible
    assert _SOURCE_REGISTRY["gent"] is GentSource


# ---------------------------------------------------------------------------
# Tenant registry: seeds, persisted file, discovery
# ---------------------------------------------------------------------------


def test_load_tenants_merges_seeds_and_discovered(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tenants_file_path(settings).write_text(
        '{"tenants": [{"municipality": "Kortrijk", "slug": "kortrijk"}]}',
        encoding="utf-8",
    )
    tenants = load_tenants(settings)
    assert "kortrijk" in tenants
    for seed in settings.consultatie_seed_tenants:
        assert seed in tenants
    assert tenants == sorted(tenants)


def test_load_tenants_survives_corrupt_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tenants_file_path(settings).write_text("{not json", encoding="utf-8")
    tenants = load_tenants(settings)
    assert set(settings.consultatie_seed_tenants) == set(tenants)


_GEMEENTEN_PAGE_1 = {
    "gemeenten": [
        {"gemeentenaam": {"geografischeNaam": {"spelling": "Antwerpen", "taal": "nl"}}},
        {"gemeentenaam": {"geografischeNaam": {"spelling": "Fakegem", "taal": "nl"}}},
    ],
    "volgende": "https://api.basisregisters.vlaanderen.be/v2/gemeenten?status=inGebruik&limit=100&offset=100",
}
_GEMEENTEN_PAGE_2 = {
    "gemeenten": [
        {
            "gemeentenaam": {
                "geografischeNaam": {"spelling": "Heist-op-den-Berg", "taal": "nl"}
            }
        },
    ],
}


@pytest.mark.asyncio
async def test_discover_tenants_probes_and_persists(tmp_path: Path) -> None:
    """Discovery enumerates municipalities from basisregisters (paginated),
    probes each slug candidate, and persists only live tenants as JSON."""
    settings = _settings(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            url__regex=r"https://api\.basisregisters\.vlaanderen\.be/v2/gemeenten\?status=inGebruik&limit=100$"
        ).mock(return_value=Response(200, json=_GEMEENTEN_PAGE_1))
        mock.get(
            url__regex=r"https://api\.basisregisters\.vlaanderen\.be/v2/gemeenten\?.*offset=100"
        ).mock(return_value=Response(200, json=_GEMEENTEN_PAGE_2))
        # Live tenants answer with the platform page; non-tenants 404
        mock.get(f"{_site('antwerpen')}/burger/nl/OpenbareOnderzoeken").mock(
            return_value=Response(200, text=ANTWERPEN_INDEX)
        )
        mock.get(f"{_site('heist-op-den-berg')}/burger/nl/OpenbareOnderzoeken").mock(
            return_value=Response(200, text=HASSELT_INDEX)
        )
        mock.get(url__regex=r"https://fakegem\.consultatieomgeving\.net/.*").mock(
            return_value=Response(404)
        )

        tenants = await discover_tenants(settings)

    assert tenants == [
        {"municipality": "Antwerpen", "slug": "antwerpen"},
        {"municipality": "Heist-op-den-Berg", "slug": "heist-op-den-berg"},
    ]
    path = tenants_file_path(settings)
    assert path.exists()
    data = path.read_text(encoding="utf-8")
    assert '"antwerpen"' in data
    assert '"heist-op-den-berg"' in data
    assert "fakegem" not in data
    # The sweep now includes the discovered tenants
    merged = load_tenants(settings)
    assert "heist-op-den-berg" in merged
