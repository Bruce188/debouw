# debouw

Belgian permit risk monitor. Research prototype that ingests Belgian *omgevingsvergunning* (environmental permit) dossiers from public sources, computes rule-based risk indicators, and surfaces them in a Streamlit dashboard.

Not legal advice. Not an MER substitute. See [LIMITATIONS.md](LIMITATIONS.md).

## Sources

| Source | Status |
|--------|--------|
| Gent consultatieomgeving | active |
| Geopunt WMS/WFS | active |
| Nominatim (OSM) | active |
| Onroerend Erfgoed WFS | active |
| RvVb rechtspraak | active |
| Inzageloket Vlaanderen | active (Playwright) |
| Brussels OpenPermits | active |

All scrapers identify as `debouw-research/0.x` and honor per-source rate limits. Full ToS posture in `LIMITATIONS.md`.

## Stack

Python 3.12–3.13, Typer CLI, SQLAlchemy + aiosqlite, Alembic, LanceDB (vector store), pdfplumber, Streamlit + Folium, Playwright (Inzageloket).

## Setup

```bash
uv sync --python 3.12                    # pinned pyarrow<22 has no 3.14 wheels
cp .env.example .env                     # set NOMINATIM_USER_AGENT contact + API keys
uv run playwright install chromium       # Inzageloket only
uv run alembic upgrade head
uv run debouw status
```

## Usage

```bash
uv run debouw ingest --source gent --limit 50
uv run debouw classify
uv run debouw export --format json --out data/out.json
uv run debouw serve --port 8501          # Streamlit dashboard
uv run debouw backfill-rvvb
uv run debouw reparse-brussels
uv run debouw eval
```

## Layout

```
debouw/        package (cli, pipeline, ingest, risk, storage, ui, models)
alembic/       migrations
scripts/       operator probes, e.g. probe_inzageloket.py (not shipped)
docs/          workflow artifacts (not shipped)
tests/         pytest suite
data/, lancedb/   runtime state under ~/debouw/ by default, incl. SQLite DB at data/debouw.sqlite (gitignored)
```

## Tests

```bash
uv sync --extra dev
uv run pytest
```

## Privacy

No `applicant_name` is persisted (Gent does not expose it publicly). Raw HTML retained for audit; linkage by `external_id` only. See `LIMITATIONS.md` § GDPR.
