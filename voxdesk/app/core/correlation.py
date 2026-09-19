"""Correlation scoping for log/trace stitching (Step 7 observability).

Two correlation layers already exist: the HTTP request id
(``app.core.errors`` middleware binds ``request_id`` on every request) and the
per-call identifiers threaded through the voice pipeline. This module adds the
missing glue — a scope that binds call-shaped fields into the structlog
context for the lifetime of one call, so *every* log line a call produces
(provider selection, fallback, TTS normalisation, usage, crash) carries the
same ``call_sid``/``call_id``/``tenant_id`` and can be pulled together into
one trace with a single grep.

The fields here are **correlation ids, not content**: they are bound into log
context (which already flows through the structlog ``merge_contextvars``
processor), and are deliberately *not* Prometheus labels — that is the
cardinality boundary the observability design draws.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import structlog

#: Longest value accepted. Anything longer is truncated — a hostile or buggy
#: value must not bloat log lines.
_MAX_LEN = 128


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text[:_MAX_LEN]


@contextmanager
def correlation_scope(**fields: object) -> Iterator[None]:
    """Bind correlation fields for the duration of a scope, always restored.

    Usage::

        with correlation_scope(call_sid=sid, call_id=str(call.id)):
            ...  # every log line here carries the fields

    Safe to nest. Values are stringified and bounded in length; keys are
    expected to be the fixed vocabulary the codebase already uses
    (``call_sid``, ``call_id``, ``tenant_id``, ``request_id``).
    """
    cleaned = {
        key: value for key, value in ((k, _clean(v)) for k, v in fields.items())
        if value is not None
    }
    if not cleaned:
        yield
        return
    structlog.contextvars.bind_contextvars(**cleaned)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*cleaned.keys())


def current_correlation() -> dict[str, str]:
    """The correlation fields currently bound, for callers that need to read
    them back (e.g. to attach to a queued task)."""
    ctx = structlog.contextvars.get_contextvars()
    return {k: v for k, v in ctx.items() if isinstance(v, str)}
