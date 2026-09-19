"""Prometheus metrics for VoxDesk.

Exposes:

    voxdesk_http_requests_total{method,path,status}
    voxdesk_http_request_duration_seconds{method,path}
    voxdesk_active_calls            (gauge, tracked by the telephony layer)
    voxdesk_provider_errors_total{provider,category}
    voxdesk_db_up                   (gauge, 1/0)

The /metrics endpoint is disabled unless METRICS_ENABLED=true. When
METRICS_TOKEN is set the scrape must present it (Bearer or ?token=), which
keeps the endpoint off the public internet while the Prometheus server in the
compose network scrapes it.
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.core.config import settings

REQUESTS = Counter(
    "voxdesk_http_requests_total",
    "HTTP requests",
    ["method", "path", "status"],
)
DURATION = Histogram(
    "voxdesk_http_request_duration_seconds",
    "Request latency",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ACTIVE_CALLS = Gauge("voxdesk_active_calls", "Live voice calls in flight")
#: Voice-provider failures, labelled by provider (closed set: deepgram /
#: elevenlabs / openai / anthropic / google / llm / unknown) and category
#: (the closed ProviderError.CATEGORIES set). Bounded by construction — never
#: label it with anything open-ended like a call SID or a model string.
PROVIDER_ERRORS = Counter(
    "voxdesk_provider_errors_total",
    "Voice-provider (STT/TTS/LLM) failures by provider and category",
    ["provider", "category"],
)
DB_UP = Gauge("voxdesk_db_up", "Database reachability (1/0)")

#: External side-effect lifecycle (Step 6, scale-compliance). Both labels are
#: bounded closed sets -- `kind` is a fixed vocabulary and `outcome` is a fixed
#: vocabulary -- so this can never mint unbounded Prometheus series the way a
#: tenant id or call SID label would.
SIDE_EFFECT_KINDS = frozenset({
    "sms",              # Twilio SMS send (reminders / owner notifications)
    "outbound_call",    # Twilio outbound dial
    "reminder",         # reminder delivery (claim/lease + send)
    "crm_sync",         # CRM delivery worker
    "message_webhook",  # inbound SMS/WhatsApp webhook
    "knowledge_ingest", # knowledge document ingestion / embedding
})
SIDE_EFFECT_OUTCOMES = frozenset({
    "attempt", "success", "failure", "retry", "duplicate", "reconciled",
})
SIDE_EFFECTS = Counter(
    "voxdesk_external_side_effects_total",
    "External side-effect lifecycle by kind and outcome",
    ["kind", "outcome"],
)


def record_side_effect(kind: str, outcome: str, *, n: int = 1) -> None:
    """Bump a side-effect counter. Unknown labels are dropped, never added."""
    if kind not in SIDE_EFFECT_KINDS or outcome not in SIDE_EFFECT_OUTCOMES:
        return
    SIDE_EFFECTS.labels(kind=kind, outcome=outcome).inc(n)

_SKIP_PREFIXES = ("/metrics", "/health")

# High-cardinality paths collapse into a small set of labels.
_PATH_LABELS = {
    "/api/tenants": "/api/tenants",
    "/auth/login": "/auth/login",
    "/auth/refresh": "/auth/refresh",
    "/telephony/voice": "/telephony/voice",
    "/telephony/status": "/telephony/status",
    "/telephony/ws": "/telephony/ws",
    "/channels/message": "/channels/message",
    "/health/ready": "/health/ready",
}


def _label_for(path: str) -> str:
    return _PATH_LABELS.get(path, "other")


def add_metrics_middleware(app: FastAPI) -> None:
    if not settings.metrics_enabled:
        return

    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        if request.url.path.startswith(_SKIP_PREFIXES):
            return await call_next(request)
        label = _label_for(request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            REQUESTS.labels(request.method, label, response.status_code).inc()
            return response
        finally:
            DURATION.labels(request.method, label).observe(
                time.perf_counter() - started
            )


def _scrape_authorized(request: Request) -> bool:
    if not settings.metrics_token:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == settings.metrics_token
    return request.query_params.get("token") == settings.metrics_token


def add_metrics_endpoint(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        if not settings.metrics_enabled:
            return Response(status_code=404, content="metrics disabled")
        if not _scrape_authorized(request):
            return Response(status_code=401, content="unauthorized")
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def set_db_up(value: bool) -> None:
    DB_UP.set(1 if value else 0)
