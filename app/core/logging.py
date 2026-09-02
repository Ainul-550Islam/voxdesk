"""Structured logging. In voice apps you MUST log timings or you can never debug latency."""
import logging
import time
from contextlib import contextmanager

import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

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

