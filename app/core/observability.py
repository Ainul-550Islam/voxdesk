"""Step 7 observability: the metric surface operators actually need.

This extends ``app.core.metrics`` (HTTP + provider errors + side effects)
with the *call-, cost-, and job-shaped* signals that answer the operator
questions in ``docs/OBSERVABILITY.md``. It introduces no second metric
system: everything here registers on the same Prometheus default registry the
existing ``/metrics`` endpoint scrapes.

Cardinality discipline (the whole point of the module)
------------------------------------------------------
Every label is a **bounded closed set** defined as a ``frozenset``. Unknown
values are normalised to ``other`` or dropped, never added as a fresh label
value. High-cardinality identifiers — call id, call SID, tenant id, phone
number, request id, user text — are **never** labels; they belong in the
structured log / correlation fields instead. A misbehaving integration or a
tenant-supplied model string therefore cannot mint unbounded series.

Cost honesty
------------
No price is ever invented. ``record_cost`` takes an explicit unit price in
millicents; a missing or zero price marks the *quantity* as UNKNOWN and the
dollar counters are left untouched. Nothing here can present an estimate as
an invoiced amount — that boundary is ``app.billing``, untouched.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ------------------------------------------------------------------ calls ---

#: Terminal call outcomes. Mirrors ``CallStatus`` (COMPLETED/FAILED/NO_ANSWER);
#: anything else collapses to ``unknown``.
CALL_OUTCOMES = frozenset({"completed", "failed", "no_answer", "unknown"})

CALLS_TOTAL = Counter(
    "voxdesk_calls_total",
    "Calls by final (terminal) outcome. Incremented exactly once per call.",
    ["outcome"],
)

#: A call was actually answered (RINGING -> IN_PROGRESS). The "call start
#: success" signal: answered / (answered + no_answer + failed).
CALLS_ANSWERED = Counter(
    "voxdesk_calls_answered_total",
    "Calls that reached an answered/in-progress state.",
)

#: Customer-visible call duration. ``sum`` is total talk seconds (divide by
#: 60 for minutes); ``count`` is finished calls.
CALL_DURATION = Histogram(
    "voxdesk_call_duration_seconds",
    "Duration of finished calls.",
    buckets=(5, 15, 30, 45, 60, 120, 180, 300, 600, 1800, 3600),
)

# ----------------------------------------------------------------- AI usage ---

#: LLM providers that may appear on the token counter's label. Mirrors the
#: closed set in ``app.agent.llm_factory.SUPPORTED_PROVIDERS``.
LLM_PROVIDERS = frozenset({"openai", "anthropic", "google", "other"})

LLM_TOKENS = Counter(
    "voxdesk_llm_tokens_total",
    "LLM tokens consumed by provider (provider-reported usage).",
    ["provider"],
)

TTS_CHARS = Counter(
    "voxdesk_tts_chars_total",
    "Characters sent to the TTS provider (ElevenLabs).",
)

STT_CHARS = Counter(
    "voxdesk_stt_chars_total",
    "Characters received from the STT provider (Deepgram).",
)

# ------------------------------------------------------------------- jobs ---

#: The scheduler's loops. Bounded: adding a loop is a code change, not data.
JOBS = frozenset({
    "reminders",
    "campaigns",
    "knowledge",
    "crm_sync",
    "billing_reconciliation",
    "retention",
})

JOB_OUTCOMES = frozenset({"success", "failure"})

JOB_RUNS = Counter(
    "voxdesk_job_runs_total",
    "Scheduler loop iterations by job and outcome.",
    ["job", "outcome"],
)

#: Unix timestamp of the last successful iteration of each job. A dashboard
#: alert compares this to now(); a job that stops succeeding stops moving.
JOB_LAST_SUCCESS = Gauge(
    "voxdesk_job_last_success_timestamp_seconds",
    "Unix timestamp of the last successful scheduler iteration per job.",
    ["job"],
)

# ------------------------------------------------------------ stuck effects ---

#: Kinds of side effect that can be observed stuck in an in-flight state.
STUCK_KINDS = frozenset({
    "crm_sync",
    "knowledge_document",
    "reminder",
    "outbound_call",
})

STUCK_SIDE_EFFECTS = Gauge(
    "voxdesk_stuck_side_effects",
    "Side effects stuck in an in-flight state past their recovery window.",
    ["kind"],
)

# -------------------------------------------------------------------- cost ---

#: Costed resources. Bounded vocabulary, aligned with UsageMetric.
COST_RESOURCES = frozenset({
    "voice_minute",
    "sms_segment",
    "llm_token",
    "tts_character",
})

#: Dollars of provider cost actually incurred, only for priced resources.
COST_USD = Counter(
    "voxdesk_cost_usd_total",
    "Provider cost in dollars, only for resources with a configured price.",
    ["resource"],
)

#: Quantity of consumption for which no price was configured (UNKNOWN).
COST_UNKNOWN_UNITS = Counter(
    "voxdesk_cost_unknown_total",
    "Consumption (in resource units) with no configured price.",
    ["resource"],
)

#: Total consumption in resource units, priced or not. Lets an operator
#: derive effective per-unit cost (COST_USD / COST_UNITS) where known.
COST_UNITS = Counter(
    "voxdesk_cost_units_total",
    "Total consumption in resource units, priced or not.",
    ["resource"],
)

# ------------------------------------------------------------------- redis ---

REDIS_UP = Gauge("voxdesk_redis_up", "Redis reachability (1/0)")


# ---------------------------------------------------------------- helpers ---

def _label(value: str | None, known: frozenset, fallback: str) -> str:
    value = (value or "").strip().lower()
    return value if value in known else fallback


def record_call_outcome(outcome: str | None) -> None:
    """Count one terminal call outcome. Unknown outcomes collapse to
    ``unknown``."""
    CALLS_TOTAL.labels(_label(outcome, CALL_OUTCOMES, "unknown")).inc()


def record_call_answered() -> None:
    CALLS_ANSWERED.inc()


def observe_call_duration(seconds: float | None) -> None:
    if seconds is None:
        return
    CALL_DURATION.observe(max(0.0, float(seconds)))


def record_llm_tokens(provider: str | None, tokens: int) -> None:
    if tokens <= 0:
        return
    LLM_TOKENS.labels(_label(provider, LLM_PROVIDERS, "other")).inc(int(tokens))


def record_tts_chars(chars: int) -> None:
    if chars > 0:
        TTS_CHARS.inc(int(chars))


def record_stt_chars(chars: int) -> None:
    if chars > 0:
        STT_CHARS.inc(int(chars))


def record_job_run(job: str, *, ok: bool) -> None:
    """Count one scheduler iteration. Unknown jobs are dropped, not added."""
    if job not in JOBS:
        return
    JOB_RUNS.labels(job, "success" if ok else "failure").inc()
    if ok:
        import time

        JOB_LAST_SUCCESS.labels(job).set(time.time())


def set_stuck_side_effects(counts: dict[str, int]) -> None:
    """Publish the stuck-count snapshot. Unknown kinds are dropped."""
    for kind, count in counts.items():
        if kind in STUCK_KINDS:
            STUCK_SIDE_EFFECTS.labels(kind).set(max(0, int(count)))


def record_cost(resource: str, units: float, *, unit_price_millicents: float | None) -> None:
    """Record consumption against the cost counters.

    ``units`` is the resource's natural unit (minutes for voice, segments for
    SMS, tokens for LLM, characters for TTS). ``unit_price_millicents`` is the
    operator-configured price per unit; ``None``/``0`` means UNKNOWN and only
    the unknown/volume counters move. No price is ever invented here.
    """
    if resource not in COST_RESOURCES or units <= 0:
        return
    COST_UNITS.labels(resource).inc(units)
    if not unit_price_millicents or unit_price_millicents <= 0:
        COST_UNKNOWN_UNITS.labels(resource).inc(units)
        return
    COST_USD.labels(resource).inc(units * unit_price_millicents / 100_000.0)


def set_redis_up(value: bool) -> None:
    REDIS_UP.set(1 if value else 0)


# ----------------------------------------------------- stuck-count snapshot ---

async def stuck_side_effect_counts(session) -> dict[str, int]:
    """Read-only counts of side effects stuck in-flight past their windows.

    The *recovery* is the reapers' job (``crm.service.reap_stuck_syncs``,
    ``knowledge.ingest.reap_stuck_documents``, the reminder lease reaper).
    This only observes, so it can run on the scheduler's tick without ever
    mutating a row. Imports are lazy so ``app.core.observability`` stays free
    of model imports.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.core.config import settings
    from app.db.models import (
        CrmSync,
        CrmSyncStatus,
        DocumentStatus,
        KnowledgeDocument,
    )

    # Naive UTC, matching the model columns (and the reapers they mirror).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    crm_cutoff = now - timedelta(minutes=max(1, settings.crm_stuck_sync_minutes))
    doc_cutoff = now - timedelta(seconds=900)

    crm_stuck = (
        await session.execute(
            select(func.count(CrmSync.id)).where(
                CrmSync.status == CrmSyncStatus.PROCESSING,
                CrmSync.last_attempt_at.isnot(None),
                CrmSync.last_attempt_at < crm_cutoff,
            )
        )
    ).scalar_one() or 0

    doc_stuck = (
        await session.execute(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.status == DocumentStatus.PROCESSING,
                KnowledgeDocument.processing_started_at.isnot(None),
                KnowledgeDocument.processing_started_at < doc_cutoff,
            )
        )
    ).scalar_one() or 0

    return {"crm_sync": int(crm_stuck), "knowledge_document": int(doc_stuck)}
