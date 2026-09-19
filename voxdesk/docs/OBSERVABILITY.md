# VoxDesk — Observability architecture (Step 7)

This document is the operator's map of how VoxDesk is measured, watched and
debugged. It is written for the person on call at 2am, not for the person who
wrote the code. It deliberately extends the existing Prometheus / Grafana /
Sentry / structlog stack rather than introducing a second monitoring system.

Contents

1. [Architecture](#architecture)
2. [Correlation: tracing one call](#correlation-tracing-one-call)
3. [Log fields](#log-fields)
4. [Metrics catalog](#metrics-catalog)
5. [Health: liveness vs readiness](#health-liveness-vs-readiness)
6. [Dashboards](#dashboards)
7. [Cardinality rules (the contract)](#cardinality-rules-the-contract)
8. [What this deliberately does not measure](#what-this-deliberately-does-not-measure)

---

## Architecture

```
                    ┌──────────────────────────────┐
  load balancer ───▶│  api (uvicorn, N workers)    │──▶ PostgreSQL
   /health/ready    │  • Prometheus registry A     │──▶ Redis (cache/rate limit)
                    │  • structlog → stdout/JSON   │
                    │  • Sentry (opt-in)           │
                    └──────────────┬───────────────┘
                                   │ /metrics (token-gated)
                    ┌──────────────▼───────────────┐
                    │  prometheus  (scrape 15s)    │
                    │  • alert rules               │
                    │  • SLO recording rules       │
                    └──────────────┬───────────────┘
                    ┌──────────────▼───────────────┐
                    │  grafana                     │
                    │  • "VoxDesk Overview"        │
                    └──────────────────────────────┘

  ┌──────────────────────────────┐
  │  scheduler (single process)  │   ──▶ PostgreSQL / Twilio / CRM
  │  • Prometheus registry B     │        providers (side effects)
  │  • /metrics on its own port  │
  └──────────────┬───────────────┘
                 │ /metrics (SCHEDULER_METRICS_PORT)
                 └──▶ prometheus (second scrape job)
```

Two processes produce metrics, each with its own Prometheus registry and its
own scrape target:

| Process | Registry contents | Scrape target |
|---|---|---|
| `api` | HTTP requests/duration, provider errors, side effects, call outcomes/duration, LLM/TTS/STT usage, cost, DB/Redis gauges | `api:8000` |
| `scheduler` | job runs/last-success, stuck side effects | `scheduler:8001` (`SCHEDULER_METRICS_PORT`) |

Logs are structured JSON in production (`LOG_FORMAT=json`), console in
development, always through `app/core/logging.py`. Sentry is opt-in
(`SENTRY_DSN`), with `send_default_pii=False` and a 0.1 trace sample in
production — never always-on tracing.

---

## Correlation: tracing one call

The operators' hardest question is "what happened on *this one* call?". The
answer is correlation, not a label:

* **HTTP requests** carry `X-Request-ID` (generated or honoured from the
  caller, sanitised). Every request log line and every error response carries
  it. See `app/core/errors.py`.
* **Voice calls** bind `call_sid`, `call_id` and `tenant_id` into the structlog
  context for the lifetime of the pipeline via
  `app/core/correlation.correlation_scope`. Every line a call produces —
  provider selection, fallback, TTS normalisation, tool calls, the
  `call.usage` summary, crashes — therefore carries the same three fields.

To pull one call's story together:

```bash
# everything about one call, in order:
grep '"call_sid": "CA…"' /var/log/voxdesk/*.log
# everything about one HTTP request:
grep '"request_id": "…"' /var/log/voxdesk/*.log
```

The call-level AI usage trace is the `call.usage` log line (STT characters,
TTS characters, LLM tokens, provider/model), emitted at call end.

**Why these are log fields and never Prometheus labels:** a call id, SID,
tenant id, phone number, request id or user text has unbounded cardinality.
Putting one in a label would grow Prometheus series without limit and take
down the metrics server. The rule is structural: bounded label, log field, or
nothing.

---

## Log fields

`app/core/logging.py` renders structlog records. Conventions:

* `event` — a short, snake_case, grep-able action name (`call.incoming`,
  `call_state.transition`, `billing.usage_recorded`, `scheduler.crm_sync`).
* `stage`/`ms` — timings via `log.timed("stage")`.
* Correlation fields: `request_id`, `call_sid`, `call_id`, `tenant_id`.
* Context fields: `tenant` (name), `provider`, `category`, `outcome`,
  `call_sid`, `node`, `reason`.

**Redaction** (hard rule): a processor runs on every record before rendering.
It masks (a) fields whose name is in a closed secret set (`token`, `password`,
`api_key`, `authorization`, `cookie`, …) and (b) the *values* of the
configured secrets wherever they appear. Never log an API key, token, cookie,
auth header, webhook secret, private message body, or raw payment data. A
phone number is logged only as `phone.redact()`.

---

## Metrics catalog

Status markers:

* **MEASURED** — the value comes from real observed data (a counter bumped by
  the code, a provider-reported number).
* **ESTIMATED** — a documented, deterministic approximation (currently none in
  the cost path; kept as a vocabulary for future use).
* **UNKNOWN** — deliberately not fabricated; the volume is counted but no
  value is asserted.

### HTTP (existing, `app/core/metrics.py`)

| Metric | Labels | Status | Answers |
|---|---|---|---|
| `voxdesk_http_requests_total` | `method`, `path` (bounded), `status` | MEASURED | request volume, error rate |
| `voxdesk_http_request_duration_seconds` | `method`, `path` | MEASURED | latency p50/p95 |

### Providers (existing)

| Metric | Labels | Status | Answers |
|---|---|---|---|
| `voxdesk_provider_errors_total` | `provider`, `category` (both bounded) | MEASURED | which provider is failing, how |
| `voxdesk_external_side_effects_total` | `kind`, `outcome` (both bounded) | MEASURED | side-effect lifecycle, retries |

### Calls (Step 7, `app/core/observability.py`)

| Metric | Type | Labels | Status | Answers |
|---|---|---|---|---|
| `voxdesk_calls_total` | counter | `outcome` ∈ {completed, failed, no_answer, unknown} | MEASURED | call outcomes; failure rate |
| `voxdesk_calls_answered_total` | counter | — | MEASURED | **call start success** (answered vs no-answer/failed) |
| `voxdesk_call_duration_seconds` | histogram | — | MEASURED | talk-time distribution; sum/60 = talk minutes |
| `voxdesk_active_calls` | gauge | — | MEASURED | live media-stream pipelines |

### AI usage (Step 7)

| Metric | Type | Labels | Status | Answers |
|---|---|---|---|---|
| `voxdesk_llm_tokens_total` | counter | `provider` ∈ {openai, anthropic, google, other} | MEASURED | LLM token volume (provider-reported) |
| `voxdesk_tts_chars_total` | counter | — | MEASURED | TTS (ElevenLabs) characters |
| `voxdesk_stt_chars_total` | counter | — | MEASURED | transcription (Deepgram) characters |

### Cost (Step 7, see `docs/COST-AWARENESS.md`)

| Metric | Type | Labels | Status | Answers |
|---|---|---|---|---|
| `voxdesk_cost_usd_total` | counter | `resource` ∈ {voice_minute, sms_segment, llm_token, tts_character} | MEASURED only when a price is configured | dollars of provider cost |
| `voxdesk_cost_unknown_total` | counter | `resource` | MEASURED (volume) | consumption with no configured price |
| `voxdesk_cost_units_total` | counter | `resource` | MEASURED | total consumption in resource units |

### Jobs & dependencies (Step 7, scheduler registry)

| Metric | Type | Labels | Status | Answers |
|---|---|---|---|---|
| `voxdesk_job_runs_total` | counter | `job`, `outcome` (both bounded) | MEASURED | scheduler loop health |
| `voxdesk_job_last_success_timestamp_seconds` | gauge | `job` | MEASURED | is a loop still succeeding? |
| `voxdesk_stuck_side_effects` | gauge | `kind` ∈ {crm_sync, knowledge_document, reminder, outbound_call} | MEASURED | stuck external side effects |
| `voxdesk_db_up` | gauge | — | MEASURED | database reachability |
| `voxdesk_redis_up` | gauge | — | MEASURED | Redis reachability (when configured) |

---

## Health: liveness vs readiness

Two endpoints, two different questions. See `app/core/health.py`.

| Endpoint | Question | Fails when | Kills liveness? |
|---|---|---|---|
| `GET /health` | is the process up? | never (except process death) | — |
| `GET /health/ready` | can this node serve traffic? | DB unreachable; Redis configured-but-unreachable; production without any voice provider configured | no — a dependency failure must not make a node look dead |

Readiness body (200 or 503):

```json
{
  "status": "ready",
  "checks": {
    "database": {"ok": true},
    "redis": {"ok": true, "configured": false},
    "providers": {"ok": false, "missing": ["deepgram"]}
  }
}
```

Rules that keep readiness cheap and honest:

* **No provider API call per request.** The `providers` check only verifies
  that credentials are *configured* (a free settings read). Live provider
  reachability is handled at call time (fail-fast) and by the CRM periodic
  health check — never on the probe.
* **Redis is required only when `REDIS_URL` is set** — otherwise the
  in-process cache is in use and there is nothing to probe.
* **Missing voice providers fail readiness only in production.** A dev/test
  box runs without live keys by design; failing its probe would make CI and
  local tooling pointlessly red.

---

## Dashboards

`observability/grafana/dashboards/voxdesk.json` ("VoxDesk Overview"):

* Row 1 — request rate, 5xx errors, p95 latency, DB/Redis/active-call gauges.
* Row 2 — availability SLO (`voxdesk:slo:availability:5m`), provider errors,
  call outcomes.
* Row 3 — LLM tokens by provider, provider cost (known $) and unpriced
  consumption (UNKNOWN), scheduler job staleness, stuck side effects.

---

## Cardinality rules (the contract)

Every label is a **bounded closed set** (a `frozenset` in the code). Unknown
values are normalised to `other`/`unknown` or dropped — never added as a fresh
label value. High-cardinality identifiers (request id, tenant id, call id,
call SID, session id, job id, event id, phone number, customer text) are log
fields, never labels. A misbehaving integration cannot mint unbounded series.

---

## What this deliberately does not measure

* **Per-tenant dashboards** — tenant identity is a log field, not a label, to
  keep cardinality bounded. Tenant-level analytics live in
  `/api/analytics/*` (SQL aggregation), not Prometheus.
* **Revenue** — plan overage is business data in the billing layer; the
  `voxdesk_cost_*` metrics are *provider cost*, never conflated with what
  tenants pay.
* **Full text of calls** — transcripts are data, not telemetry. Only counts
  and durations are measured.
