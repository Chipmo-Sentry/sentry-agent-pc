"""structlog setup — pretty console for dev, JSON for prod."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

from sentry_agent_pc.settings import get_settings


def configure_logging() -> None:
    settings = get_settings()
    # Single source of truth for the level — keep basicConfig and structlog's
    # filtering wrapper consistent so LOG_LEVEL=DEBUG actually shows DEBUG lines.
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
