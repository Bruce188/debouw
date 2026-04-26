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
    geopunt_base: str = "https://geo.api.vlaanderen.be"
    nominatim_base: str = "https://nominatim.openstreetmap.org"
    rvvb_base: str = "https://www.dbrc.be/rechtspraak"
    inzageloket_base: str = "https://omgevingsloketinzage.omgeving.vlaanderen.be"
    openpermits_brussels_base: str = "https://openpermits.brussels"
    onroerend_erfgoed_base: str = "https://geo.onroerenderfgoed.be"
    inspirepub_waterinfo_base: str = "https://inspirepub.waterinfo.be"

    # --- Throttle rates (seconds per request) ---
    throttle_gent_seconds: float = 2.0
    throttle_nominatim_seconds: float = 1.0
    throttle_rvvb_seconds: float = 3.0
    throttle_inzageloket_seconds: float = 5.0
    throttle_geopunt_seconds: float = 1.0

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
    engine_version: str = "0.2.0-rules-v1"
    log_format: Literal["json", "console"] = "console"

    # --- Identification (Inzageloket robots policy) ---
    # ToS compliance: Nominatim requires a meaningful User-Agent with contact info.
    # Set NOMINATIM_USER_AGENT in .env to supply the maintainer's actual contact address.
    # The default here is a placeholder — override it before sending real requests.
    nominatim_user_agent: str = "debouw-research/0.x (set NOMINATIM_USER_AGENT in .env)"

    # --- LLM narrator settings ---
    sonnet_model: str = "claude-sonnet-4-5-20250929"
    openai_fallback_model: str = "gpt-4o-2024-08-06"
    narration_cache_enabled: bool = True
    narration_max_tokens: int = 1024

    # --- API keys (resolved from .env, never persisted) ---
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
