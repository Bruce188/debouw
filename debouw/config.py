"""
Application configuration via pydantic-settings.

Instantiate Settings() at command time (e.g., in cli.py). Do not instantiate
at module level — this module exports the class only.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Source base URLs ---
    gent_consultatie_base: str = "https://gent.consultatieomgeving.net/burger"
    basisregisters_base: str = "https://api.basisregisters.vlaanderen.be"
    geopunt_base: str = "https://geo.api.vlaanderen.be"
    nominatim_base: str = "https://nominatim.openstreetmap.org"
    rvvb_base: str = "https://www.dbrc.be"
    inzageloket_base: str = "https://omgevingsloketinzage.omgeving.vlaanderen.be"
    openpermits_brussels_base: str = "https://openpermits.brussels"
    onroerend_erfgoed_base: str = "https://geo.onroerenderfgoed.be"
    inspirepub_waterinfo_base: str = "https://inspirepub.waterinfo.be"
    # MercatorNet public host (departement Omgeving) — statewide Flanders
    # omgevingsvergunning layers + Natura 2000. Open data, licence
    # "modellicentie gratis hergebruik v1.0" (free reuse). Consumers append
    # /raadpleegdienstenmercatorpubliek/wfs themselves (host-only base, like
    # the other *_base settings).
    mercator_base: str = "https://www.mercator.vlaanderen.be"

    # --- Throttle rates (seconds per request) ---
    throttle_gent_seconds: float = 2.0
    throttle_nominatim_seconds: float = 1.0
    throttle_rvvb_seconds: float = 10.0  # bumped to honour robots.txt Crawl-Delay
    throttle_inzageloket_seconds: float = 5.0
    throttle_geopunt_seconds: float = 1.0
    throttle_brussels_seconds: float = 2.0  # Phase 5: polite rate for openpermits.brussels
    throttle_waterinfo_seconds: float = 2.0  # inspirepub.waterinfo.be (ESRI REST overlays)
    throttle_mercator_seconds: float = 2.0  # www.mercator.vlaanderen.be (WFS: dossiers + Natura 2000)
    # Global rate across ALL consultatieomgeving.net tenants (shared platform,
    # one throttled client serves the whole multi-tenant sweep).
    throttle_vlaanderen_consultatie_seconds: float = 2.0
    throttle_basisregisters_seconds: float = 1.0  # open-data API, polite default

    # --- Mercator (statewide Flanders WFS) harvest sizing ---
    mercator_page_size: int = 100  # WFS result-paging count per GetFeature
    # Modest default cap when `debouw ingest` is run without --limit; a full
    # statewide harvest (~22k active dossiers) is opt-in: pass an explicit
    # large --limit, or set MERCATOR_DEFAULT_LIMIT=0 (<=0 → unlimited).
    mercator_default_limit: int = 200
    mercator_iioa_join: bool = True  # join lu_omv_iioa_v2 → iioa_class

    # --- Vlaanderen consultatie (Remmicom multi-tenant platform) ---
    # ~110 Flemish municipalities run the identical "Consultatie Module Burger"
    # at https://<slug>.consultatieomgeving.net/burger/nl/OpenbareOnderzoeken.
    consultatie_site_template: str = "https://{slug}.consultatieomgeving.net"
    # Verified live tenants (2026-06-12) — the sweep always covers these; the
    # full list comes from `debouw discover-tenants` (persisted JSON, merged in).
    consultatie_seed_tenants: list[str] = Field(
        default_factory=lambda: [
            "aalter",
            "antwerpen",
            "gent",
            "hasselt",
            "heist-op-den-berg",
            "sint-pieters-leeuw",
        ]
    )
    # Persisted discovery output; None → <data_root>/consultatie_tenants.json
    consultatie_tenants_file: Path | None = None

    # --- Paths (lazy-created at runtime) ---
    data_root: Path = Field(
        default_factory=lambda: Path.home() / "debouw" / "data"
    )
    db_path: Path = Field(
        default_factory=lambda: Path.home() / "debouw" / "data" / "debouw.sqlite"
    )
    lancedb_path: Path = Field(
        default_factory=lambda: Path.home() / "debouw" / "lancedb"
    )

    # --- Engine + logging ---
    engine_version: str = "0.7.1"
    enable_iioa_heuristic: bool = True  # env: ENABLE_IIOA_HEURISTIC; gates MER + IIOA auto-derive
    log_format: Literal["json", "console"] = "console"

    # --- Identification (Inzageloket robots policy) ---
    # ToS compliance: Nominatim requires a meaningful User-Agent with contact info.
    # Set NOMINATIM_USER_AGENT in .env to supply the maintainer's actual contact address.
    # The default here is a placeholder — override it before sending real requests.
    nominatim_user_agent: str = "debouw-research/0.x (set NOMINATIM_USER_AGENT in .env)"

    # --- LLM narrator settings ---
    sonnet_model: str = "claude-sonnet-4-6"
    openai_fallback_model: str = "gpt-4o-2024-08-06"
    narration_cache_enabled: bool = True
    narration_max_tokens: int = 1024

    # --- Phase 3: RvVb backfill + precedent corpus ---
    rvvb_backfill_root: Path = Field(
        default_factory=lambda: Path.home() / "debouw" / "data" / "raw" / "rvvb"
    )
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 3072
    lancedb_arrests_table: str = "rvvb_arrests"
    sonnet_extraction_concurrency: int = 8
    sonnet_extraction_model: str = "claude-sonnet-4-5-20250929"
    precedent_search_k: int = 8
    precedent_search_threshold: float = 0.50
    gold_set_min_n: int = 30
    precedent_alpha: float = 0.4
    arrest_extractor_version: str = "0.1"

    # --- API keys (resolved from .env, never persisted) ---
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
