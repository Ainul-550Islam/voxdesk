# VoxDesk — SLOs and alerts (Step 7)

Two questions, one document: what are we promising (SLO), and what wakes a
human when we are about to break it (alerts).

---

## SLOs

The one availability SLO VoxDesk states, measured by the recording rules in
`observability/slos.yml`:

| SLO | Target | Measured by | Window |
|---|---|---|---|
| HTTP availability | ≥ 99.9% monthly | `voxdesk:slo:availability:30d` (5m for operations) | 5m / 30d |

**Definition.** Availability = `1 − (5xx responses / all responses)`. A 4xx is
*correct* behaviour (auth failure, not-found), not a miss, so it is excluded
from the numerator. This is process-level HTTP availability — the standard
definition. Call-level quality has its own signals (`voxdesk_calls_total`,
`voxdesk_calls_answered_total`) and is not folded into this number.

**Reading it.**

```promql
# current availability (5-minute window):
voxdesk:slo:availability:5m

# 30-day availability (meaningful only after 30 days of retained data —
# before that the window is optimistically short):
voxdesk:slo:availability:30d

# error budget remaining in a window, as a fraction of traffic:
voxdesk:slo:availability:5m - 0.999
```

**Honesty about the 30d window.** A fresh Prometheus has no 30 days of data,
so `:30d` reads optimistically high until the window fills. The 5m window is
the operations number; the 30d window is the report number.

---

## Alerts

`observability/alerts.yml`. Minimal and high-value: every alert means "a human
should look now", and every alert carries a `runbook` annotation pointing at
its section below.

| Alert | Severity | Condition |
|---|---|---|
| VoxDeskInstanceDown | critical | API unscrapeable 2m |
| VoxDeskSchedulerDown | critical | scheduler unscrapeable 2m |
| VoxDeskDatabaseDown | critical | `voxdesk_db_up == 0` 1m |
| VoxDeskHighErrorRate | warning | 5xx > 5% over 5m |
| VoxDeskSlowP95 | warning | p95 > 2s over 5m |
| VoxDeskCallFailureRate | warning | > 30% of finished calls failed over 10m |
| VoxDeskProviderErrorBurst | warning | > 10 provider errors in 10m |
| VoxDeskStuckSideEffects | critical | any stuck side effect for 10m |
| VoxDeskJobStale | warning | frequent scheduler loop silent > 1h |

### Runbooks

#### VoxDeskInstanceDown
* **What it means:** Prometheus cannot scrape `/metrics` from the API.
* **Check:** `docker compose -f docker-compose.prod.yml ps api`; `docker
  compose logs api --tail 200`; is the process OOM-killed? Is Caddy up?
* **Act:** restart the service; if it loops, roll back
  (`scripts/deploy.sh` at the last-known-good SHA) and diagnose from logs.

#### VoxDeskSchedulerDown
* **What it means:** the background worker (reminders, campaigns, CRM sync,
  billing reconciliation, retention, stuck sweep) is unscrapeable.
* **Impact:** calls still work; side effects stop flowing — reminders stop,
  CRM writes queue up, usage summaries go stale.
* **Act:** check `docker compose logs scheduler`; restart it. Its loops are
  idempotent and self-healing, so a restart is safe.

#### VoxDeskDatabaseDown
* **What it means:** `/health/ready` is reporting the database unreachable,
  and the load balancer is (or should be) draining this node.
* **Act:** check `pg_isready`, disk space, connection count
  (`pool_size`/`max_overflow` exhaustion). Do **not** restart the API hoping
  to fix the DB — readiness is doing its job.

#### VoxDeskHighErrorRate / VoxDeskSlowP95
* **What it means:** either a code regression or a saturated dependency.
* **Act:** open the Overview dashboard; correlate the 5xx/latency spike with a
  deploy time. Pull the worst `request_id`s from logs
  (`grep '"request_id"'`) and reproduce. Roll back if it follows a deploy.

#### VoxDeskCallFailureRate
* **What it means:** more than 30% of finished calls are `failed` (not
  `no_answer` — those are "nobody picked up", a business fact, not a fault).
* **Act:** open the Provider errors panel; group failures by
  provider/category. A single provider outage shows as one category spiking.
  Check `call.crashed` and `pipeline.run_failed` logs by `call_sid`.

#### VoxDeskProviderErrorBurst
* **What it means:** a voice provider (Deepgram/ElevenLabs/LLM) is failing at
  volume.
* **Act:** identify the provider/category from the dashboard; check its status
  page; verify keys/limits. The LLM factory falls back between providers, so
  an LLM burst may be invisible to callers while it degrades quality.

#### VoxDeskStuckSideEffects
* **What it means:** reminders, CRM syncs or document ingestions have been
  in-flight past their recovery window and the reapers have not recovered
  them.
* **Act:** check `scheduler.crm_sync`, `scheduler.reminders`,
  `ingest.reaped_stuck_documents` logs; confirm the scheduler is alive
  (VoxDeskSchedulerDown). A stuck row blocks the customer-visible action
  (their reminder, their CRM lead) — treat as urgent.

#### VoxDeskJobStale
* **What it means:** one of the frequent loops (reminders, campaigns,
  CRM sync, knowledge) has not *succeeded* in over an hour. (Retention and
  billing reconciliation run daily/hourly and are deliberately not in this
  alert.)
* **Act:** inspect the loop's error logs; the loops swallow per-iteration
  errors to survive, so a persistent failure shows as a silent stale gauge
  rather than a crash.

---

## Alerting philosophy

No hundreds of noisy rules. Each alert is a condition an operator can act on,
each has a severity and a runbook. If a new alert cannot state its runbook
action in one sentence, it does not ship.
