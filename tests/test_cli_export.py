"""
CLI tests for `debouw export` (portfolio export command).

Seeds a file-backed SQLite DB with the raw-SQL pattern established in
tests/test_repository.py (read-seam section), points the CLI at it via the
DB_PATH env var (pydantic-settings: actual env beats .env), and drives the
command through typer.testing.CliRunner.
"""

import csv
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from debouw.cli import app
from debouw.storage.schema import Base

runner = CliRunner()

EXPECTED_HEADER = (
    "external_id,source,region,municipality,status,overall_score,"
    "expected_delay_days,confidence,engine_version,n_risk_factors,"
    "top_risk_category,first_seen_at"
)


# ---------------------------------------------------------------------------
# Seeding helpers (pattern: tests/test_repository.py read-seam section)
# ---------------------------------------------------------------------------


def _insert_project(
    conn,
    external_id: str,
    *,
    municipality: str = "Gent",
    source: str = "gent_consultatie",
    region: str = "vl",
    first_seen: str = "2026-04-01 10:00:00",
) -> None:
    conn.execute(
        text(
            "INSERT INTO permit_projects (external_id, source, region, "
            "omv_reference, detail_url, title, address, status, attachments, "
            "dossier_pdfs, raw_html_path, first_seen_at, last_changed_at, "
            "content_hash, decision_regime) "
            "VALUES (:eid, :source, :region, :eid, "
            "'https://example.test/dossier', 'Export test project', :address, "
            "'in_public_inquiry', '[]', '[]', '/tmp/export.html', :seen, :seen, "
            ":hash, 'post_2026_reform')"
        ),
        {
            "eid": external_id,
            "source": source,
            "region": region,
            "address": json.dumps(
                {"raw": "Teststraat 1", "municipality": municipality}
            ),
            "seen": first_seen,
            "hash": "c" * 64,
        },
    )


def _insert_assessment(
    conn,
    eid: str,
    engine_version: str,
    score: float,
    generated_at: str,
    top_risks: str = "[]",
) -> None:
    conn.execute(
        text(
            "INSERT INTO risk_assessments (project_external_id, engine_version, "
            "overall_score, expected_delay_days, confidence, summary, top_risks, "
            "calibration_regime, generated_at, inputs_hash) "
            "VALUES (:eid, :ver, :score, 30.0, 0.5, 'samenvatting', :risks, "
            "'post_2026_reform', :gen, :hash)"
        ),
        {
            "eid": eid,
            "ver": engine_version,
            "score": score,
            "risks": top_risks,
            "gen": generated_at,
            "hash": "d" * 64,
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """File DB with three projects: two assessed (one of them twice), one bare."""
    db_file = tmp_path / "export-test.sqlite"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _insert_project(conn, "gent:OMV_EXPORT_1", municipality="Gent")
        _insert_project(
            conn,
            "brussels:01/PU/777",
            municipality="Bruxelles",
            source="brussels_openpermits",
            region="brussels",
        )
        _insert_project(conn, "gent:OMV_EXPORT_NOASSESS", municipality="Deinze")
        # Older assessment inserted alongside a newer one — export must pick
        # the latest (generated_at DESC) per project.
        _insert_assessment(
            conn, "gent:OMV_EXPORT_1", "0.6.0", 0.2, "2026-05-01 12:00:00"
        )
        _insert_assessment(
            conn,
            "gent:OMV_EXPORT_1",
            "0.7.0",
            0.8,
            "2026-06-01 12:00:00",
            top_risks='[{"category": "water_flood"}, {"category": "mer_screening"}]',
        )
        _insert_assessment(
            conn, "brussels:01/PU/777", "0.7.0", 0.3, "2026-06-01 12:00:00"
        )
    engine.dispose()
    monkeypatch.setenv("DB_PATH", str(db_file))
    return db_file


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """File DB with the full schema materialized but zero rows."""
    db_file = tmp_path / "export-empty.sqlite"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("DB_PATH", str(db_file))
    return db_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_csv_happy_path(seeded_db: Path, tmp_path: Path) -> None:
    """CSV export: header, latest-assessment values, file written where pointed."""
    out = tmp_path / "portfolio.csv"
    result = runner.invoke(app, ["export", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert "exported 3 rows" in result.output
    assert str(out) in result.output
    assert out.exists(), "CSV file not written where --out pointed"

    content = out.read_text(encoding="utf-8")
    assert content.splitlines()[0] == EXPECTED_HEADER

    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3

    by_id = {r["external_id"]: r for r in rows}
    gent = by_id["gent:OMV_EXPORT_1"]
    # Latest assessment (0.7.0 / 0.8) wins over the older 0.6.0 / 0.2 row.
    assert gent["overall_score"] == "0.8"
    assert gent["engine_version"] == "0.7.0"
    assert gent["n_risk_factors"] == "2"
    assert gent["top_risk_category"] == "water_flood"
    assert gent["municipality"] == "Gent"
    assert gent["status"] == "in_public_inquiry"
    assert gent["region"] == "vl"

    # Highest risk sorts first.
    assert rows[0]["external_id"] == "gent:OMV_EXPORT_1"


def test_csv_unassessed_project_has_blank_assessment_fields(
    seeded_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "portfolio.csv"
    result = runner.invoke(app, ["export", "--out", str(out)])
    assert result.exit_code == 0, result.output

    with out.open(newline="", encoding="utf-8") as fh:
        by_id = {r["external_id"]: r for r in csv.DictReader(fh)}
    bare = by_id["gent:OMV_EXPORT_NOASSESS"]
    assert bare["overall_score"] == ""
    assert bare["engine_version"] == ""
    assert bare["n_risk_factors"] == ""
    assert bare["top_risk_category"] == ""
    assert bare["municipality"] == "Deinze"


def test_json_mode(seeded_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "portfolio.json"
    result = runner.invoke(app, ["export", "--format", "json", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert "exported 3 rows" in result.output
    assert out.exists()

    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 3

    by_id = {r["external_id"]: r for r in data}
    gent = by_id["gent:OMV_EXPORT_1"]
    assert gent["overall_score"] == 0.8
    assert gent["engine_version"] == "0.7.0"
    assert gent["n_risk_factors"] == 2
    assert gent["top_risk_category"] == "water_flood"
    assert set(gent) == set(EXPECTED_HEADER.split(","))

    brussels = by_id["brussels:01/PU/777"]
    assert brussels["region"] == "brussels"
    assert brussels["n_risk_factors"] == 0
    assert brussels["top_risk_category"] is None

    bare = by_id["gent:OMV_EXPORT_NOASSESS"]
    assert bare["overall_score"] is None


def test_min_score_filter(seeded_db: Path, tmp_path: Path) -> None:
    """--min-score drops low scores AND unassessed projects."""
    out = tmp_path / "filtered.csv"
    result = runner.invoke(
        app, ["export", "--min-score", "0.5", "--out", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert "exported 1 rows" in result.output

    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["external_id"] for r in rows] == ["gent:OMV_EXPORT_1"]


def test_empty_db_friendly_message(empty_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "never-written.csv"
    result = runner.invoke(app, ["export", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert "nothing to export" in result.output
    assert not out.exists(), "no file should be written for an empty portfolio"


def test_default_out_filename(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --out the file lands at ./export-YYYYMMDD.csv."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export"])

    assert result.exit_code == 0, result.output
    expected = tmp_path / f"export-{date.today():%Y%m%d}.csv"
    assert expected.exists()


def test_invalid_format_rejected(seeded_db: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["export", "--format", "xml", "--out", str(tmp_path / "x.xml")]
    )
    assert result.exit_code != 0
    assert not (tmp_path / "x.xml").exists()
