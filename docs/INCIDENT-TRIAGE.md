# VoxDesk — Incident triage (Step 7)

The five-minute path from "something is wrong" to "we know what and how bad".
Complements `docs/INCIDENT-RESPONSE.md` (roles/severity/notification); this is
the *diagnostic* playbook.

---

## 1. Orient: which surface is broken?

| Symptom | First dashboard panel | First log grep |
|---|---|---|
| Everything down | Instance down / DB down alerts | `docker compose ps` |
| Calls fail to start | Call outcomes + Answered | `"call.crashed"`, `"call.e2e_rejected"` |
| Calls start but are silent/wrong | Provider errors, active calls | `"pipeline.run_failed"` |
| Calls work, business data lags | Job staleness, stuck side effects | `"scheduler."` |
| Bills look wrong | (billing layer) | `"billing."` |

Every alert in `observability/alerts.yml` carries a `runbook` annotation
pointing at the matching section of `docs/SLO-ALERTS.md` — start there.

## 2. Scope it with correlation

A single failing call is traced by its correlation fields, which are bound
into every log line the call produces (`app/core/correlation.py`):

```bash
grep '"call_sid": "CA…"' /var/log/voxdesk/*.log     # the whole call
grep '"request_id": "…"' /var/log/voxdesk/*.log     # one HTTP request
```

The `call.usage` line at call end gives STT chars / TTS chars / LLM tokens and
the provider/model, so "was the LLM even called?" is answered without
reconstructing the audio.

## 3. Classify the failure by label

Provider failures are grouped by bounded `provider`/`category` labels:

```promql
sum by (provider, category) (increase(voxdesk_provider_errors_total[15m]))
```

* `rate_limit` → the tenant or VoxDesk hit a provider quota.
* `timeout`/`unavailable` → provider-side latency/outage.
* `configuration_error`/`authentication_error` → keys or tenant config.
* `other` → something unclassified; the `call.crashed` log has the details.

Call failures are `failed` vs `no_answer`:

```promql
sum by (outcome) (increase(voxdesk_calls_total[15m]))
```

`no_answer` is a business fact (nobody picked up); `failed` is a fault worth
digging into.

## 4. Check the dependencies

```bash
curl -s http://localhost:8000/health/ready
```

The readiness body names the failing dependency (`database`, `redis`,
`providers`). A 503 here while `/health` is 200 means the process is alive but
should not be receiving traffic — the load balancer is doing its job.

## 5. Cost and blast radius

```promql
sum by (resource) (increase(voxdesk_cost_usd_total[1h]))
```

Tells you what the incident is costing in provider fees (only for resources
with a configured price — see `docs/COST-AWARENESS.md`). Compare with the
volume in `voxdesk_cost_unknown_total` for the unpriced remainder.

## 6. Confirm recovery

* Alerts return to OK.
* `voxdesk:slo:availability:5m` returns to target.
* `voxdesk_job_last_success_timestamp_seconds` starts advancing again.
* `voxdesk_stuck_side_effects` back to 0.

Record the timeline, root cause and one follow-up control in the incident
channel (see `docs/INCIDENT-RESPONSE.md`).
