# STEP 5 pre-work: audit of the existing CRM code

Written **before** anything was modified. This is what was actually there, not
what the README claimed.

## What existed

One file: `app/integrations/crm.py`, 130 lines. Three tenant columns:
`crm_webhook_url`, `crm_type` (free-form string), `crm_api_key`. One caller:
`app/telephony/twilio_handler.py::_push_to_crm`, fired from the Twilio status
callback. Six tests in `tests/test_outbound.py`.

## Findings

| # | Finding | Severity | Evidence |
|---|---|---|---|
| 1 | **It is a webhook, not a CRM integration.** `push()` POSTs to `tenant.crm_webhook_url` — a URL the tenant supplies — for *every* provider. No provider API base URL exists anywhere in the codebase. | Fundamental | `crm.py:109-118` |
| 2 | **"GoHighLevel support" never calls GoHighLevel.** `to_gohighlevel()` builds a GHL-shaped contact body and then posts it to the tenant's webhook URL. Correct payload, wrong destination. | Fundamental | `crm.py:112` + `:118` |
| 3 | **HubSpot has headers but no mapping.** `_headers()` has a `hubspot` branch, so a bearer token is attached, but the body posted is VoxDesk's internal event shape. HubSpot's `/crm/v3/objects/contacts` would reject it. | High | `crm.py:70-71`, `:112` |
| 4 | **Jobber does not exist.** No code, no config, no mention. | — | grep |
| 5 | **Credentials are stored in plaintext.** `Tenant.crm_api_key: String(255)`, no encryption, no separate table. Anyone with a DB read has every tenant's CRM token. | Critical | `models.py:165` |
| 6 | **Provider error bodies are logged.** `log.warning("crm.rejected", body=resp.text[:200])` writes whatever the provider returned into the log — which for a misconfigured auth endpoint frequently echoes the submitted token. | High | `crm.py:124` |
| 7 | **"Retries" do not wait.** `for attempt in range(retries + 1)` with no `sleep`, no backoff, no jitter. Three requests fire back to back in microseconds, which is the worst possible response to a 429 or a cold provider. | High | `crm.py:115-128` |
| 8 | **Failure is not persisted.** `push()` returns `False` and the boolean is logged. There is no row recording that a sync failed, no attempt count, no error, and therefore no possible manual retry. A failed sync is silently lost. | Critical | `crm.py:130`, `twilio_handler.py:410` |
| 9 | **The duplicate guard loses events.** `call.crm_synced = True` is committed *before* the POST is attempted. Process dies between the two → the call is permanently marked synced and never sent. It protects against duplicates by risking total loss. | Critical | `twilio_handler.py:335-337` |
| 10 | **No idempotency toward the provider.** No idempotency key, no external-ID lookup. A retry after a provider timeout that actually succeeded creates a second contact. | High | — |
| 11 | **Fire-and-forget task is not retained.** `asyncio.create_task(...)` with no reference held; CPython may garbage-collect the task mid-flight. Also runs inside the web process, so CRM latency competes with call webhooks. | Medium | `twilio_handler.py:337` |
| 12 | **Only one event type exists.** `"event": "call.completed"`, hard-coded in `build_payload`. No missed call, no lead, no appointment, no transfer. | High | `crm.py:40` |
| 13 | **`crm_type` is an unvalidated string.** Anything that is not exactly `"gohighlevel"` falls through to the generic branch, so a typo silently changes the payload shape instead of erroring. | Medium | `crm.py:112` |
| 14 | **Full transcripts are in the payload contract.** `build_payload(transcript=...)` puts raw turns in the body with no policy switch. Not currently passed by the caller, but the contract invites it. | Medium | `crm.py:34`, `:59` |
| 15 | **No outbound signing.** No HMAC, no timestamp, no replay protection. A receiver cannot verify the POST came from VoxDesk. | High | — |
| 16 | **No inbound webhook handling at all.** Nothing receives provider callbacks, so there is nothing to forge — but also no way for a CRM to push back. | — | — |
| 17 | **No per-tenant rate limiting.** One tenant whose provider is rate-limiting them would, under any real retry loop, consume the shared worker. | Medium | — |

## Direct answers to the questions the brief asked

**What current CRM functionality exists?**
A single best-effort HTTP POST of a flat JSON call summary, fired once per
completed call, to a tenant-configured URL.

**What is only a webhook?**
All of it. Including both things labelled "GoHighLevel" and "HubSpot".

**Are credentials stored?**
Yes — `Tenant.crm_api_key`, plaintext, unencrypted, in the tenants table.

**Do retries exist?**
Nominally. `retries=2` loops immediately with no delay and no persistence, so
in practice it is one burst of three requests and then silence.

**Are duplicate events possible?**
Duplicate *sends* are prevented by `call.crm_synced`, but at the cost of
silently dropping the event if the process dies at the wrong moment. Duplicate
*CRM records* are entirely possible, because nothing is idempotent at the
provider end.

**Is tenant isolation enforced?**
Incidentally. Credentials hang off the `Tenant` row and the only caller already
holds a tenant, so there is no cross-tenant path today — but there is also no
integration table, no `tenant_id` on any sync record, and no external-ID
storage, so the isolation is a property of there being almost nothing to
isolate.

## What STEP 5 keeps

`build_payload`, `to_gohighlevel`, `_headers` and `push` stay reachable as
`app.integrations.crm.legacy` and remain re-exported from
`app.integrations.crm`, so the six existing tests in `tests/test_outbound.py`
pass unmodified. Nothing new calls them.

