"""Structured logging. In voice apps you MUST log timings or you can never debug latency."""
import logging
import time
from contextlib import contextmanager

import structlog

from app.core.config import settings


def _choose_renderer(log_format: str, is_production: bool):
    """JSON in production (machine readable); coloured console in development.

    LOG_FORMAT can force either renderer; by default production gets JSON and
    development gets the console renderer.
    """
    use_json = (log_format or "").lower() == "json" or is_production
    if use_json:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()


def _configure_logging() -> None:
    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _choose_renderer(settings.log_format, settings.is_production),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


_configure_logging()

log = structlog.get_logger()


@contextmanager
def timed(label: str, **ctx):
    """Usage:  with timed("stt"): ...   -> logs elapsed ms"""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.info("timing", stage=label, ms=round(elapsed_ms, 1), **ctx)
