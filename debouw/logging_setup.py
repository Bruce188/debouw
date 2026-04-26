"""
Structured logging setup via structlog.

Call configure_logging(settings) once at CLI startup. Idempotent — safe
to call multiple times in tests. No module-level side effects.
"""

import logging

import structlog

from debouw.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog with JSON or console renderer based on settings."""

    # Common pre-chain processors (always applied)
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    # Format-specific renderer
    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
