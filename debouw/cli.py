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
    typer.echo(f"nominatim_user_agent: {settings.nominatim_user_agent}")
    typer.echo(f"gent_consultatie_base: {settings.gent_consultatie_base}")
    typer.echo(f"nominatim_base: {settings.nominatim_base}")
    typer.echo(f"rvvb_base: {settings.rvvb_base}")
    typer.echo(f"inzageloket_base: {settings.inzageloket_base}")


@app.command()
def ingest(source: str = typer.Option(..., help="Source name")) -> None:
    """Ingest permit data from a source."""
    typer.echo("Not yet implemented in Phase 0; see master plan § Phase 1 (Gent), Phase 4 (Inzageloket), Phase 5 (Brussels)")


@app.command()
def classify(project_id: str | None = typer.Option(None)) -> None:
    """Classify risk for a permit project."""
    typer.echo("Not yet implemented in Phase 0; see master plan § Phase 2")


@app.command()
def export(format: str = typer.Option("json"), output: str | None = typer.Option(None)) -> None:
    """Export permit data."""
    typer.echo("Not yet implemented in Phase 0; see master plan § Phase 1 (CLI surface)")


@app.command()
def serve() -> None:
    """Launch the Streamlit dashboard."""
    typer.echo("Not yet implemented in Phase 0; see master plan § Phase 1 (Streamlit)")


@app.command(name="backfill-rvvb")
def backfill_rvvb(years: str = typer.Option("2022,2023,2024,2025")) -> None:
    """Backfill RvVb arrest corpus."""
    typer.echo("Not yet implemented in Phase 0; see master plan § Phase 3")
