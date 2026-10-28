from pathlib import Path

import typer

from debouw.config import Settings
from debouw.logging_setup import configure_logging

app = typer.Typer(name="debouw", help="Belgian permit risk monitoring")


@app.command()
def status() -> None:
    """Print loaded config + DB path + LanceDB path + engine version."""
    settings = Settings()
    configure_logging(settings)
    typer.echo(f"engine_version: {settings.engine_version}")
    typer.echo(f"db_path: {settings.db_path}")
    typer.echo(f"lancedb_path: {settings.lancedb_path}")
    typer.echo(f"data_root: {settings.data_root}")
    typer.echo(f"log_format: {settings.log_format}")
    typer.echo(f"throttle_gent_seconds: {settings.throttle_gent_seconds}")
    typer.echo(f"throttle_nominatim_seconds: {settings.throttle_nominatim_seconds}")
    typer.echo(f"throttle_rvvb_seconds: {settings.throttle_rvvb_seconds}")
    typer.echo(f"throttle_inzageloket_seconds: {settings.throttle_inzageloket_seconds}")
    typer.echo(f"throttle_geopunt_seconds: {settings.throttle_geopunt_seconds}")
    typer.echo(f"nominatim_user_agent: {settings.nominatim_user_agent}")
    typer.echo(f"gent_consultatie_base: {settings.gent_consultatie_base}")
    typer.echo(f"nominatim_base: {settings.nominatim_base}")
    typer.echo(f"rvvb_base: {settings.rvvb_base}")
    typer.echo(f"inzageloket_base: {settings.inzageloket_base}")


@app.command()
def ingest(
    source: str = typer.Option(
        "gent",
        help="Source name (gent | vlaanderen_consultatie | vlaanderen_inzage | brussels | vlaanderen_mercator)",
    ),
    limit: int | None = typer.Option(None, help="Max dossiers to ingest"),
) -> None:
    """Ingest permit data from a source."""
    import asyncio
    from debouw.pipeline import run
    result = asyncio.run(run(source=source, limit=limit))
    typer.echo(
        f"ingested {result.ingested} projects, {result.overlays} overlays, "
        f"{result.assessments} assessments"
    )


@app.command(name="discover-tenants")
def discover_tenants_cmd(
    limit: int | None = typer.Option(
        None, help="Probe only the first N municipalities (None = all ~285)"
    ),
) -> None:
    """Probe Flemish municipalities for live consultatieomgeving.net tenants.

    Enumerates municipalities from api.basisregisters.vlaanderen.be, probes
    https://<slug>.consultatieomgeving.net at 1 req/2 s with the identified
    User-Agent, and persists the live tenant list as JSON under data/.
    The `vlaanderen_consultatie` ingest source merges this list with the
    seed tenants in config. Full sweep takes ~10 minutes.
    """
    import asyncio
    from debouw.ingest.sources.consultatie import discover_tenants, tenants_file_path

    settings = Settings()
    configure_logging(settings)
    tenants = asyncio.run(discover_tenants(settings, limit=limit))
    typer.echo(
        f"discovered {len(tenants)} live tenants -> {tenants_file_path(settings)}"
    )


@app.command()
def classify(
    project_id: str | None = typer.Option(None, help="External id; classifies all when None"),
    reclassify_all: bool = typer.Option(False, help="Re-run even if assessment exists"),
) -> None:
    """Classify risk for permit projects."""
    import asyncio
    from debouw.risk.engine import classify_all
    settings = Settings()
    configure_logging(settings)
    n = asyncio.run(classify_all(settings, project_id=project_id, force=reclassify_all))
    typer.echo(f"classified {n} projects")


_EXPORT_COLUMNS = [
    "external_id",
    "source",
    "region",
    "municipality",
    "status",
    "overall_score",
    "expected_delay_days",
    "confidence",
    "engine_version",
    "n_risk_factors",
    "top_risk_category",
    "first_seen_at",
]


@app.command()
def export(
    format: str = typer.Option("csv", help="Output format (csv | json)"),
    out: Path | None = typer.Option(
        None, "--out", help="Output path (default: ./export-YYYYMMDD.<format>)"
    ),
    min_score: float | None = typer.Option(
        None, help="Only include projects whose latest overall_score >= this value"
    ),
) -> None:
    """Export the portfolio: one row per project with its latest risk assessment.

    Uses the synchronous read seam (debouw/storage/read.py) — SQLite WAL mode
    permits this reader to run safely alongside a concurrent writer.
    """
    import csv
    import json
    from datetime import date as _date

    from debouw.storage.read import latest_assessments, list_projects, make_sync_engine

    fmt = format.lower()
    if fmt not in ("csv", "json"):
        raise typer.BadParameter("must be 'csv' or 'json'", param_hint="--format")

    settings = Settings()
    configure_logging(settings)

    engine = make_sync_engine(settings)
    try:
        projects = list_projects(engine)
        assessments = latest_assessments(engine)
    finally:
        engine.dispose()

    rows: list[dict] = []
    for p in projects:
        a = assessments.get(p["external_id"])
        if min_score is not None and (a is None or a["overall_score"] < min_score):
            continue
        address = p.get("address")
        if not isinstance(address, dict):
            address = {}
        top_risks = (a or {}).get("top_risks")
        if not isinstance(top_risks, list):
            top_risks = []
        rows.append(
            {
                "external_id": p["external_id"],
                "source": p["source"],
                "region": p["region"],
                "municipality": address.get("municipality"),
                "status": p["status"],
                "overall_score": a["overall_score"] if a else None,
                "expected_delay_days": a["expected_delay_days"] if a else None,
                "confidence": a["confidence"] if a else None,
                "engine_version": a["engine_version"] if a else None,
                "n_risk_factors": len(top_risks) if a else None,
                "top_risk_category": (
                    top_risks[0].get("category")
                    if top_risks and isinstance(top_risks[0], dict)
                    else None
                ),
                "first_seen_at": (
                    str(p["first_seen_at"]) if p["first_seen_at"] is not None else None
                ),
            }
        )

    if not rows:
        if min_score is not None:
            typer.echo(
                f"no projects with overall_score >= {min_score} — nothing to export"
            )
        else:
            typer.echo("no projects in database — nothing to export")
        return

    # Portfolio view: highest-risk first; unassessed projects last.
    rows.sort(
        key=lambda r: (
            r["overall_score"] is not None,
            r["overall_score"] or 0.0,
            r["first_seen_at"] or "",
        ),
        reverse=True,
    )

    out_path = out if out is not None else Path(f"export-{_date.today():%Y%m%d}.{fmt}")
    if fmt == "csv":
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    typer.echo(f"exported {len(rows)} rows to {out_path}")


@app.command()
def serve(port: int = typer.Option(8501)) -> None:
    """Launch the Streamlit dashboard."""
    import os
    app_path = Path(__file__).parent / "ui" / "app.py"
    os.execvp("streamlit", ["streamlit", "run", str(app_path), "--server.port", str(port)])


@app.command(name="backfill-rvvb")
def backfill_rvvb(
    years: str = typer.Option("2022,2023,2024,2025"),
    limit: int | None = typer.Option(None, help="Max arrests (default: unlimited)"),
) -> None:
    """Scrape + extract + embed + LanceDB-write the RvVb arrest corpus."""
    import asyncio
    from debouw.ingest.sources.rvvb import backfill_run
    settings = Settings()
    configure_logging(settings)
    year_list = [int(y) for y in years.split(",") if y.strip()]
    n = asyncio.run(backfill_run(settings, years=year_list, limit=limit))
    typer.echo(f"backfilled {n} arrests")


@app.command(name="reparse-brussels")
def reparse_brussels() -> None:
    """Re-parse raw Brussels HTML and update the database with new field extractions.

    Recipe:
        debouw reparse-brussels && debouw classify --reclassify-all

    Selects all permit_projects rows where source='brussels_openpermits', reads
    the cached raw_html_path, re-runs _parse_brussels_html, and upserts the
    updated PermitProject row. Returns count of rows touched.
    """
    import asyncio
    from debouw.storage.db import make_engine, make_sessionmaker
    from debouw.storage.repository import upsert_project
    from debouw.ingest.sources.brussels import _parse_brussels_html

    settings = Settings()
    configure_logging(settings)

    async def _run() -> int:
        from sqlalchemy import select
        engine = make_engine(settings)
        Session = make_sessionmaker(engine)
        count = 0
        try:
            async with Session() as s:
                from debouw.storage.schema import PermitProjectRow
                result = await s.execute(
                    select(PermitProjectRow).where(
                        PermitProjectRow.source == "brussels_openpermits"
                    )
                )
                rows = result.scalars().all()

            for row in rows:
                raw_html_path = row.raw_html_path
                try:
                    with open(raw_html_path, encoding="utf-8") as fh:
                        html = fh.read()
                except (OSError, TypeError) as exc:
                    typer.echo(f"warn: skip {row.external_id} — {exc}", err=True)
                    continue

                try:
                    parsed = _parse_brussels_html(html, row.external_id)
                except Exception as exc:
                    typer.echo(f"warn: parse failed {row.external_id} — {exc}", err=True)
                    continue

                # Reconstruct current project from DB row
                from debouw.storage.repository import get_project as _get
                async with Session() as s:
                    async with s.begin():
                        existing = await _get(s, row.external_id)
                        if existing is None:
                            continue
                        updated = existing.model_copy(update={
                            "error_weight": parsed["error_weight"],
                            "floor_area_m2": parsed["floor_area_m2"],
                            "mer_status": parsed["mer_status"] or existing.mer_status,
                            "case_language": parsed["case_language"],
                            "description": parsed["description"] or existing.description,
                            "units": parsed["units"] if parsed["units"] is not None else existing.units,
                            "floors": parsed["floors"] if parsed["floors"] is not None else existing.floors,
                            "iioa_class": parsed["iioa_class"] if parsed["iioa_class"] is not None else existing.iioa_class,
                            "trees_to_fell": parsed["trees_to_fell"] if parsed["trees_to_fell"] is not None else existing.trees_to_fell,
                        })
                        await upsert_project(s, updated)
                        count += 1
        finally:
            await engine.dispose()
        return count

    n = asyncio.run(_run())
    typer.echo(f"reparse-brussels: touched {n} rows")


@app.command(name="reparse-gent")
def reparse_gent() -> None:
    """Re-parse Gent address strings, geocode them, and recompute overlays.

    Recipe:
        debouw reparse-gent && debouw classify --reclassify-all

    Selects all permit_projects rows where source='gent_consultatie',
    re-parses Address components from the stored raw locatie string
    (parse_gent_address), geocodes the primary street address via the
    Vlaanderen geolocation API with a Nominatim fallback, recomputes the
    Geopunt overlays from the new point, and upserts the updated row.
    Echoes per-row parsed components, the point, and overlay field changes.
    """
    import asyncio

    from sqlalchemy import select

    from debouw.ingest.enrich_geopunt import enrich
    from debouw.ingest.geocode import geocode_with_fallback
    from debouw.ingest.sources.gent import address_geocode_query, parse_gent_address
    from debouw.models.permit import GeoOverlays
    from debouw.storage.db import make_engine, make_sessionmaker
    from debouw.storage.repository import get_project, upsert_project
    from debouw.storage.schema import PermitProjectRow

    settings = Settings()
    configure_logging(settings)

    _OVERLAY_FIELDS = (
        "in_natura_2000",
        "natura_2000_distance_m",
        "in_signaalgebied",
        "flood_risk_fluvial",
        "flood_risk_pluvial",
        "in_protected_heritage",
        "heritage_match_distance_m",
    )

    async def _run() -> int:
        engine = make_engine(settings)
        Session = make_sessionmaker(engine)
        count = 0
        try:
            async with Session() as s:
                result = await s.execute(
                    select(PermitProjectRow.external_id).where(
                        PermitProjectRow.source == "gent_consultatie"
                    )
                )
                external_ids = [r[0] for r in result.all()]

            for external_id in external_ids:
                async with Session() as s:
                    async with s.begin():
                        existing = await get_project(s, external_id)
                if existing is None:
                    continue

                parsed = parse_gent_address(existing.address.raw)
                query = address_geocode_query(parsed)
                point = (
                    await geocode_with_fallback(query, settings) if query else None
                )
                if point is None and query:
                    typer.echo(
                        f"warn: geocode failed {external_id} — q={query!r}", err=True
                    )

                overlays = await enrich(point, settings)
                old_overlays = existing.overlays or GeoOverlays()
                changed = [
                    f"{f}: {getattr(old_overlays, f)!r} -> {getattr(overlays, f)!r}"
                    for f in _OVERLAY_FIELDS
                    if getattr(old_overlays, f) != getattr(overlays, f)
                ]

                updated = existing.model_copy(
                    update={
                        "address": parsed.model_copy(update={"point": point}),
                        "overlays": overlays,
                    }
                )
                async with Session() as s:
                    async with s.begin():
                        await upsert_project(s, updated)
                count += 1
                point_str = (
                    f"({point.lat:.5f}, {point.lon:.5f})" if point else "None"
                )
                typer.echo(
                    f"{external_id}: street={parsed.street!r} "
                    f"hn={parsed.house_number!r} pc={parsed.postcode} "
                    f"muni={parsed.municipality!r} point={point_str}"
                )
                typer.echo(
                    "  overlays changed: " + ("; ".join(changed) if changed else "(none)")
                )
        finally:
            await engine.dispose()
        return count

    n = asyncio.run(_run())
    typer.echo(f"reparse-gent: touched {n} rows")


@app.command(name="eval")
def eval_cmd(
    gold_set: str = typer.Option("debouw/risk/eval/gold_set.jsonl"),
) -> None:
    """Run calibration backtest against gold_set.jsonl."""
    import asyncio
    from debouw.risk.calibration import run_calibration
    settings = Settings()
    configure_logging(settings)
    report = asyncio.run(
        run_calibration(settings, gold_set_path=Path(gold_set))
    )
    typer.echo(
        f"n={report.n}; P@5={report.p_at_5}; "
        f"Brier={report.brier}; gates={report.gates}"
    )
