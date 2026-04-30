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
    source: str = typer.Option("gent", help="Source name (gent | vlaanderen_inzage | brussels)"),
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


@app.command()
def export(format: str = typer.Option("json"), output: str | None = typer.Option(None)) -> None:
    """Export permit data."""
    typer.echo("Not yet implemented in Phase 0; see master plan § Phase 1 (CLI surface)")


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
                        })
                        await upsert_project(s, updated)
                        count += 1
        finally:
            await engine.dispose()
        return count

    n = asyncio.run(_run())
    typer.echo(f"reparse-brussels: touched {n} rows")


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
