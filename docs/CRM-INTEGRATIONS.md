# CRM integrations

VoxDesk pushes calls, leads and appointments into whatever CRM a business
already runs. This document is the contract: what the layer guarantees, how to
configure it, and where the edges are.

> **Live verification status.** No provider API was contacted with real
> credentials while building this. See [§13](#13-what-was-not-live-tested) for
> exactly what that means and what was verified instead.

---

## 1. Architecture

```
  VoxDesk call / lead / appointment
            │
            ▼
  app/integrations/crm/hooks.py        ← the only place lifecycle events enter
            │                            (never raises, never sends, never commits)
            ▼
  CrmEvent   ─ one row per business fact, written in the caller's transaction
            │
            ▼
  CrmSync    ─ one row per (event, integration); nothing sent yet
            │
            ▼
  scripts/scheduler.py :: crm_sync_loop
            │
            ▼
  app/integrations/crm/service.py      ← decrypt, build adapter, retry, persist
            │
            ▼
  providers/{ghl,hubspot,jobber,webhook}.py
            │
            ▼
  provider API  →  external_id  →  CrmSync.status = SYNCED
```

The load-bearing idea is the split between **recording** and **delivering**. A
business fact is committed once, in the same transaction as the thing that
caused it. Delivery is a separate row with its own retry budget. A process
death between "the call ended" and "the CRM knows" therefore loses nothing.

### Files

| Path | Responsibility |
|---|---|
| `crm/models.py` | Provider-neutral domain: `NormalizedContact`, `NormalizedActivity`, `NormalizedAppointment`, `CrmResult`, identity hashing |
| `crm/base.py` | `CrmProvider` base class, `Capability`, `ProviderContext`, the single HTTP entry point |
| `crm/errors.py` | Error taxonomy, HTTP→error classification, secret scrubbing |
| `crm/crypto.py` | AES-256-GCM credential envelope, key ring, rotation |
| `crm/mapping.py` | VoxDesk row → `NormalizedContact`, custom-field validation |
| `crm/events.py` | `emit()`, idempotency keys, payload builders |
| `crm/service.py` | Orchestration: claim, decrypt, deliver, retry, persist, health |
| `crm/retry.py` | `RetryPolicy`, `TokenBucketLimiter` |
| `crm/registry.py` | Provider type → adapter class |
| `crm/hooks.py` | Lifecycle entry points for telephony and the agent |
| `crm/providers/` | One module per vendor |
| `crm/legacy.py` | The pre-STEP-5 webhook helper, kept only for compatibility |
| `api/integration_routes.py` | Tenant-facing configuration API |
| `api/crm_webhook_routes.py` | Inbound provider webhooks |

### Adding a provider

1. Write `providers/salesforce.py` subclassing `CrmProvider`, declaring
   `name`, `capabilities`, and overriding the operations you support.
2. Add `SALESFORCE` to `CrmProviderType` **and to migration `0006`'s enum list**
   (the parity test in `tests/test_enum_consistency.py` enforces this).
3. Register it in `crm/registry.py`.
4. Add its credential and config field allowlists in `api/integration_routes.py`.

Nothing else changes. The contract tests in `tests/test_crm_contract.py` are
parameterized over the registry, so the new adapter is immediately tested for
tenant scoping, error normalization, timeout handling, idempotency and secret
handling.

---

## 2. Providers and capabilities

| Capability | GoHighLevel | HubSpot | Jobber | Webhook |
|---|:--:|:--:|:--:|:--:|
| `upsert_contact` | ✅ | ✅ | ✅¹ | ✅ |
| `create_contact` | ✅ | ✅ | ✅ | ✅ |
| `update_contact` | ✅ | ✅ | ✅ | — |
| `get_contact` | ✅ | ✅ | ✅ | — |
| `create_note` | ✅ | ✅ | ✅ | ✅ |
| `create_activity` | ✅ | ✅ | ✅ | ✅ |
| `create_appointment` | ✅ | — | — | ✅ |
| `cancel_appointment` | — | — | — | ✅ |
| `add_tag` | ✅ | —² | — | ✅ |
| `add_custom_fields` | ✅ | ✅ | — | ✅ |
| `health_check` | ✅ | ✅ | ✅ | ✅ |

¹ Not a native upsert — see §2.3.
² HubSpot has no tags. Declaring the capability and silently dropping the data
would be worse than not having it; the equivalent is a list membership or a
property, which a tenant can configure through field mappings.

A dashboard can read this table from `GET /api/integrations/crm/providers`
rather than hard-coding it.

### 2.1 GoHighLevel

* Base URL `https://services.leadconnectorhq.com`
* `Authorization: Bearer <token>` — OAuth access token or Private Integration Token
* **`Version: 2021-07-28` on every request.** Omitting it produces errors that
  blame the payload rather than the missing header. This is the single most
  common GHL integration failure.
* `locationId` is required on writes. An agency-level token cannot write to
  location-scoped resources; exchange it for a location token first.
* Rate limit: 500 requests / 10 seconds per sub-account.

`POST /contacts/upsert` is the primary path. HighLevel documents it, and it
matches on email or phone within the location. Community reports disagree
about whether it is present on every account and API version, so **a 404 falls
back to search-then-create/update** rather than failing the sync permanently.

GHL accepts no idempotency key header. Duplicate protection comes from the
upsert semantics plus `CrmContactLink`.

Configuration: `location_id` (required), `calendar_id` (required for
appointment sync), `base_url` (testing).

### 2.2 HubSpot

* Base URL `https://api.hubapi.com`
* `Authorization: Bearer <private app token>`
* Errors carry a `category` field which is more reliable than the status alone.

**Upsert needs two strategies, and the second one is the common path.**
HubSpot deduplicates contacts on email; phone is not a unique identifier. A
voice product usually has a phone number and no email at all. So:

1. Email present → `POST /crm/v3/objects/contacts/batch/upsert` with
   `idProperty: "email"`.
2. No email, **or** the portal rejects the upsert as non-unique → search on
   phone, then `PATCH` the match or `POST` a new contact.

Step 2's second trigger is real and documented: on portals where email is not
configured as a unique property, the upsert returns
`400 VALIDATION_ERROR: Unable to perform update/upsert by non-unique 0-1
property email`. That is the portal's configuration, not our payload, so it
falls through rather than failing.

Property names are lowercase with no separators — `firstname`, not
`first_name`. HubSpot accepts unknown properties on some plans and silently
drops them, so getting this wrong produces nameless contacts and no error.

Notes use `POST /crm/v3/objects/notes` with `hs_timestamp` in **epoch
milliseconds** and association type **202** (note → contact; 201 is the
inverse and produces an orphaned note). Note bodies render as HTML and contain
call summaries, so they are HTML-escaped.

### 2.3 Jobber

Strategically the most important adapter: Jobber is what home-service
businesses run on.

* Single endpoint `POST https://api.getjobber.com/api/graphql`
* `Authorization: Bearer <OAuth token>`, `X-JOBBER-GRAPHQL-VERSION: 2025-04-16`

**Three traps, all of which arrive as HTTP 200:**

1. `userErrors` inside the mutation payload means the mutation was **rejected**.
   An adapter that only checks status codes reports every rejection as a
   success.
2. A top-level `errors` array means the query was bad, or the service is
   throttling. Jobber throttles on query *cost*, with
   `extensions.code == "THROTTLED"`.
3. A null payload with no errors at all. Treated as a failure, because there
   is no external id to record.

**`clientUpsert` was removed** from Jobber's schema in the 2023-08-18 version,
so upsert is query-then-create/edit. **`clientNoteCreate` was also removed**;
the surviving mutation is `clientCreateNote`. They transpose easily and the
failure surfaces as a permissions-shaped error that sends you looking in the
wrong place.

**No appointment sync.** Jobber models scheduled work as jobs and visits, and
creating one requires a property, line items and a schedule this layer cannot
supply. Rather than half-writing a job, `CREATE_APPOINTMENT` is not declared,
and appointment events for Jobber record a clean `unsupported` permanent
failure with one attempt rather than five retries.

### 2.4 Generic webhook

For Zapier, Make, n8n or a bespoke endpoint. HTTPS only.

Headers on every delivery:

```
X-VoxDesk-Signature:       sha256=<hex hmac>
X-VoxDesk-Timestamp:       <unix seconds>
X-VoxDesk-Event:           lead.created
X-VoxDesk-Idempotency-Key: lead.created:3f2a...
X-VoxDesk-Delivery:        <uuid, unique per attempt>
```

Body:

```json
{
  "specversion": "1.0",
  "source": "voxdesk",
  "type": "call.completed",
  "id": "call.completed:3f2a1b4c-0000-0000-0000-000000000000",
  "time": "2026-03-01T10:30:00+00:00",
  "data": {
    "call_id": "3f2a1b4c-0000-0000-0000-000000000000",
    "call_sid": "CAfaketestsid0000000000000000000",
    "direction": "inbound",
    "duration_seconds": 92.5,
    "intent": "booking",
    "summary": "Caller asked about a cleaning and booked Tuesday.",
    "booked": true,
    "transferred": false,
    "lead_score": 80,
    "transcript_reference": "voxdesk:call:3f2a1b4c-.../turns"
  }
}
```

*All values above are fake.*

**Verifying the signature** (this is the contract a receiver implements):

```python
import hmac, hashlib, time

def verify(secret: str, raw_body: str, signature: str, timestamp: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:      # reject replays
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), f"{timestamp}.{raw_body}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)  # not ==
```

Three details that make this a signature rather than a decoration:

* **The timestamp is inside the signed string.** Signing only the body lets an
  attacker replay a captured request forever.
* **The secret is per tenant.** A shared platform secret would let any
  customer forge any other customer's events. We generate it, so an unsigned
  webhook integration cannot exist.
* **`compare_digest`, not `==`.** Short-circuit equality leaks the signature
  prefix through timing.

A receiver answering `200` with `{"ok": false}` is treated as a rejection, not
a success.

---

## 3. Credential handling

**At rest:** AES-256-GCM, application-level, in `crm_integrations.credentials_encrypted`.

```
v1.<key_id>.<base64url nonce>.<base64url ciphertext>
```

* **Authenticated.** A tampered row fails to decrypt instead of producing
  garbage that gets sent to a provider.
* **Fresh 96-bit nonce per encryption.** Never derived, never reused.
* **Key id in the envelope**, so rotation is "add a key, make it active,
  re-encrypt at leisure" rather than a flag day.
* **The AAD is `(tenant_id, provider)`.** Ciphertext copied from Tenant A's row
  into Tenant B's will not decrypt. Moving ciphertext between rows is a real
  attack against naive column encryption; this closes it.

**Threat model, stated plainly.** This defends against a database dump — a
stolen backup, a misconfigured replica, an ORM logger printing rows. It does
**not** defend against an attacker who already runs the application process;
they have the key by construction. Nothing short of an HSM or a remote KMS
changes that, and neither is in scope.

**In transit and in the app:**

* Never returned by any API. `IntegrationOut` is an allowlist, and a test
  asserts no field in it is even *named* like a secret.
* Never logged. `errors.safe_message()` scrubs anything credential-shaped from
  every string that reaches the database or a log line, and a 401 response body
  is excluded entirely rather than merely scrubbed — it is the most likely
  place for a token to be reflected.
* Never in a JWT, never in the audit `detail` (only the *names* of the fields
  supplied), never in a `ProviderContext` repr.

### Local development

```bash
# Generate a key
python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

# .env
CRM_ENCRYPTION_KEYS="dev:<that base64 value>"
```

Leaving it unset is allowed in development: the app boots, integrations simply
cannot store credentials, and `PUT /api/integrations/crm/{provider}` returns
`503` rather than silently falling back to plaintext.

### Production

`CRM_ENCRYPTION_KEYS` is a **required production secret**.
`Settings.validate_security()` refuses to boot without it, and refuses to boot
with a malformed one. Supply it from your secret manager (AWS Secrets Manager,
GCP Secret Manager, Vault), never from a file in the repo.

Rotation:

```bash
# 1. Prepend the new key; the old one stays readable.
CRM_ENCRYPTION_KEYS="2026b:<new>,2026a:<old>"
# 2. Deploy. New writes use 2026b; old rows still decrypt with 2026a.
# 3. Re-save each integration (or run a re-encrypt job) to move rows forward.
#    `credentials_key_id` is a real column, so "which rows still use 2026a"
#    is a SQL query.
# 4. Once none remain, drop 2026a.
```

---

## 4. Tenant isolation

| Mechanism | Where |
|---|---|
| `UNIQUE (tenant_id, provider)` | `crm_integrations` — makes the pair a key, not a filter |
| Every lookup is `(tenant_id, provider)` | `service.get_integration()` is the only sanctioned path |
| 404, never 403, on another tenant's row | `_owned()` — a 403 would confirm the row exists |
| Tenant comes from the verified JWT | `ctx.tenant_id`; no route reads a tenant from a body, query or header |
| `UNIQUE (tenant_id, idempotency_key)` | one tenant's key cannot suppress another's event |
| `UNIQUE (tenant_id, provider, identity_hash)` | contacts never match across tenants |
| Identity hashes are **tenant-salted** | two tenants sharing a customer produce different hashes, so even a query missing its `WHERE` could not join them |
| AES-GCM AAD binds ciphertext to its tenant | credentials cannot be moved between rows |
| Adapters hold **no database handle** | a `ProviderContext` and nothing else — an adapter *cannot* reach another tenant, by construction |

That last one is asserted structurally in `test_crm_contract.py`: an adapter's
instance dict must contain exactly `{"context"}`.

---

## 5. Event model

| Event | Entity | Emitted from |
|---|---|---|
| `call.completed` | call | `twilio_handler` status callback, terminal + COMPLETED |
| `call.missed` | call | same callback, terminal + NO_ANSWER/FAILED |
| `transfer.completed` | call | `/transfer-status`, when a human answers |
| `lead.created` | lead | bulk import, DNC capture |
| `lead.updated` | lead | status changes (discriminated by reason) |
| `appointment.booked` | appointment | `book_appointment` agent function |
| `appointment.cancelled` | appointment | cancellation flow |

Each `CrmEvent` carries an id, tenant id, entity type and id, event type,
idempotency key, payload, `payload_version`, and `created_at`.

`transfer.completed` is emitted separately from `call.completed` on purpose:
"was transferred" and "the transfer connected" are different facts, and a CRM
that conflates them tells the business somebody spoke to the customer when
nobody did.

---

## 6. Idempotency

Keys are **derived, not generated**:

```
call.completed:3f2a1b4c-0000-0000-0000-000000000000
lead.updated:9c8b...:status:qualified
```

Deterministic, so the same business fact computes the same key forever; and
legible, so the value means something in a log or a support query. A random
uuid would make every duplicate a new event — exactly the failure the
requirement describes.

`UNIQUE (tenant_id, idempotency_key)` is the guarantee; the `SELECT` before
the insert is only an optimisation, and the `IntegrityError` path handles the
race.

**The four duplicate sources, and what stops each:**

| Source | Defence |
|---|---|
| Duplicate provider webhook | `CrmWebhookReceipt` unique on `(tenant, provider, provider_event_id)` |
| Retried Twilio callback | derived idempotency key → second `emit()` is a no-op |
| Worker restart mid-flight | sync reaped from `PROCESSING` → `FAILED`, retried; the spent attempt still counts |
| **Provider timeout after acceptance** | upsert semantics + `CrmContactLink` remembering the external id, so the retry updates instead of creating |

`lead.updated` takes a discriminator, because an update genuinely recurs for
one entity. `"status:qualified"` and `"status:do_not_call"` are two events;
the same change retried is one.

---

## 7. Retry

Bounded exponential backoff with **full jitter**: attempt *n* waits a uniform
random duration in `[0, min(base · 2ⁿ⁻¹, cap)]`.

Full jitter rather than "exponential plus noise" because the former actually
decorrelates a thundering herd. Without it, a provider outage that fails a
hundred syncs at once produces a hundred retries at exactly t+2s, then t+6s —
every wave re-creating the overload.

| Retried | Not retried |
|---|---|
| timeout | invalid credentials (401/403) |
| connection reset | malformed payload / missing field (4xx) |
| 429 | unsupported operation |
| 5xx | authorization denial |

A revoked token does not come back on its own, and hammering an auth endpoint
with a dead credential is how an account gets locked — so 401 is permanent
after **one** attempt, not five.

A provider's `Retry-After` wins when it is longer than our backoff (arguing
with a rate limiter is how a token gets suspended), is capped at 300 s, and an
unparseable value falls back to our own schedule. Only delta-seconds is
honoured; parsing a remote-supplied HTTP date into a sleep duration is a small
denial of service waiting to happen.

After `CRM_RETRY_MAX_ATTEMPTS`, the sync becomes `PERMANENT_FAILURE` and waits
for a human.

**Per-tenant rate protection** is a separate token bucket per
`(tenant, provider)`. Being locally throttled is not a failure: the sync goes
back to `PENDING` with a later time and **does not spend an attempt**. Its
purpose is fairness — one tenant with broken credentials must not consume
every worker pass. It is process-local, which is correct for a single
scheduler process and would need Redis for a second one.

---

## 8. Field mapping

Built-in mapping lives in each adapter. Tenants add their own through
`field_mappings`, a `{provider_field: voxdesk_source}` object:

```json
{
  "field_mappings": {
    "ai_call_summary": "call_summary",
    "ai_lead_score": "lead_score",
    "booking_type": "appointment_type"
  }
}
```

`voxdesk_source` must be in the allowlist:

```
lead_score            call_summary        call_outcome
call_id               call_direction      call_duration_seconds
appointment_type      appointment_starts_at
source_campaign       intent              booked
transferred           recording_url       transcript_reference
```

An allowlist rather than "any attribute of the payload", so adding a source is
deliberate and no internal field can be exfiltrated by guessing its name.

Bounds: at most 25 mappings, keys ≤ 120 characters, values truncated to 2000
characters at send time. Write-time validation bounds the *configuration*;
send-time truncation bounds the *data*, because a summary long enough to
matter is generated at runtime. Invalid configuration is a `422` when it is
saved, not a mystery at three in the morning.

A missing source is **skipped**, not sent as null: a CRM field that silently
empties on every call without a summary is worse than one that is not written.

---

## 9. Transcripts

`call.completed` payloads carry a **transcript reference**, never the
transcript:

```json
"transcript_reference": "voxdesk:call:<id>:turns"
```

A call transcript is the most sensitive thing this product holds — a caller
reciting a card number, a diagnosis, an address. It reaches a provider only
when that integration has `share_transcripts: true`, and even then it is the
reference, resolvable through the authenticated VoxDesk API.

---

## 10. Configuration API

```
GET    /api/integrations/crm                        list (tenant-scoped)
GET    /api/integrations/crm/providers              catalogue + capabilities
GET    /api/integrations/crm/syncs                  sync status feed
GET    /api/integrations/crm/{provider}             one integration
PUT    /api/integrations/crm/{provider}             connect or update
DELETE /api/integrations/crm/{provider}             remove (history kept)
POST   /api/integrations/crm/{provider}/test        health check
POST   /api/integrations/crm/{provider}/disconnect  drop creds, keep config
```

Permissions: `INTEGRATION_READ` to read and test, `INTEGRATION_WRITE` to
change. Both are admin and owner only. There is not one role-string comparison
in these files, and a test asserts it.

Connect:

```bash
curl -X PUT https://api.example.com/api/integrations/crm/gohighlevel \
  -H "Authorization: Bearer $VOXDESK_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": {"access_token": "pit-fake-token-for-docs"},
    "config": {"location_id": "loc_FAKE123", "calendar_id": "cal_FAKE456"},
    "field_mappings": {"ai_call_summary": "call_summary"},
    "subscribed_events": ["call.completed", "call.missed"],
    "share_transcripts": false
  }'
```

`credentials` is write-only. Omitting it on an update leaves the stored token
untouched, so editing a location id does not require re-pasting a token the
tenant cannot read back. An empty `subscribed_events` means *all* events —
connecting a CRM and then receiving nothing until you also tick seven boxes is
a support ticket, not a feature.

`disconnect` versus `DELETE`: disconnect drops the credentials and keeps the
configuration, for token rotation or a pause during a CRM migration. `DELETE`
removes the integration but **keeps the sync history**, because those rows are
the record of what was sent to a customer's CRM.

Health check response:

```json
{"connected": false, "provider": "hubspot", "latency_ms": 142.3,
 "safe_message": "hubspot rejected the credentials (HTTP 401)"}
```

Always `200`. A tenant pressing "Test" with a dead token should get a
diagnosis, not a 500.

---

## 11. Inbound webhooks

```
POST /api/integrations/crm/inbound/{provider}/{routing_token}
```

The tenant is identified by a high-entropy `routing_token` in the **path**,
derived per integration from the platform secret. The body is never consulted
for routing, and a body field named `tenant_id` is discarded.

**The token is not the authentication.** It selects which integration to
verify against; the signature proves the request is genuine. Anyone who learns
the token still cannot forge an event — which matters, because URLs leak into
logs, browser history and support-ticket screenshots.

Replay protection is a `CrmWebhookReceipt` per
`(tenant, provider, provider_event_id)`. A second delivery returns
`200 {"duplicate": true}` — not a `409`, because a provider that sees an error
just retries. Receipts are pruned by age.

Every rejection returns exactly `401 {"detail": "Invalid signature"}`, so the
endpoint cannot be used to enumerate which tokens or providers are live.

**Currently only the `webhook` provider is verifiable.** GoHighLevel signs with
Ed25519 against a HighLevel public key; Jobber and HubSpot each have their own
scheme; none of that key material is provisioned by this step. Those providers
therefore **fail closed** — an unverified inbound endpoint is worse than no
inbound endpoint, because it is a public, tenant-addressable write path.

This endpoint verifies and records; it does not yet mutate VoxDesk data.
Acting on inbound CRM changes is bidirectional sync and out of scope. Building
the unverified half first is how webhook endpoints become incidents.

---

## 12. Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `CRM_ENCRYPTION_KEYS` | *(empty)* | **Required in production.** `id:base64key[,id:base64key]`, first is active |
| `CRM_REQUEST_TIMEOUT_SECONDS` | `10.0` | Per provider request |
| `CRM_RETRY_MAX_ATTEMPTS` | `5` | Then `PERMANENT_FAILURE` |
| `CRM_RETRY_BASE_SECONDS` | `2.0` | Backoff base |
| `CRM_RETRY_MAX_SECONDS` | `900.0` | Backoff cap |
| `CRM_RATE_LIMIT_PER_SECOND` | `5.0` | Per (tenant, provider) |
| `CRM_RATE_LIMIT_BURST` | `20.0` | Token bucket size |
| `CRM_SYNC_INTERVAL_SECONDS` | `20` | Worker poll |
| `CRM_SYNC_BATCH_SIZE` | `20` | Syncs per pass |
| `CRM_STUCK_SYNC_MINUTES` | `15` | Reaper threshold |
| `CRM_WEBHOOK_TOLERANCE_SECONDS` | `300` | Inbound timestamp window |

The worker runs inside `scripts/scheduler.py` alongside the reminder, campaign
and knowledge-ingestion loops. No second job system was introduced.

---

## 13. What was not live-tested

**No provider API was contacted with real credentials.** Every adapter is
written against the published API contract and exercised against a scripted
HTTP transport that intercepts `httpx` — so header assembly, payload shape,
status classification and timeout handling are all real code paths, but no
byte left the machine.

Not verified live:

* **GoHighLevel** — no account, no location id, no token. In particular the
  `/contacts/upsert` availability question in §2.1 is unresolved; the fallback
  exists precisely because it could not be settled by testing.
* **HubSpot** — no portal. The non-unique-email upsert rejection is handled
  from documentation and community reports, not from an observed response.
* **Jobber** — no developer account, no OAuth app. The `userErrors` shape,
  the throttling code and the mutation names come from Jobber's published
  schema and changelog.
* **Generic webhook** — no live receiver. The signature scheme is verified
  against its own reference implementation, which proves internal consistency,
  not third-party interoperability.
* **Inbound webhooks** from any real provider.
* **PostgreSQL.** All tests run on SQLite. `alembic upgrade head` has not been
  executed; migration/model parity is checked by static AST parsing, and
  tables are created via `Base.metadata.create_all`.

What *was* verified: serialization for every provider, error mapping across
all four adapters, retry behaviour, idempotency including the
timeout-after-acceptance case, tenant isolation, credential encryption, and
the full configuration API. 351 tests.

---

## 14. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `PUT` returns `503 "encryption is not configured"` | `CRM_ENCRYPTION_KEYS` unset |
| Sync stuck `PENDING`, never attempted | scheduler not running, or the tenant is locally rate-limited — check `next_attempt_at` |
| `PERMANENT_FAILURE` / `unauthorized` after one attempt | token revoked or wrong scopes. Correct behaviour: reconnect |
| `PERMANENT_FAILURE` / `unsupported` | the provider has no such operation. See the capability table |
| `PERMANENT_FAILURE` / `misconfigured` | missing `location_id`, `calendar_id` or webhook URL |
| GHL: errors blaming the payload | almost always the missing `Version` header — but this adapter always sends it, so suspect an agency token that needs exchanging for a location token |
| HubSpot contacts with no name | property names must be `firstname`/`lastname`; HubSpot drops unknown properties silently |
| HubSpot: duplicate contacts per call | expected if callers have no email and phone search is failing; check the search filter |
| Jobber: "field was hidden due to permissions" | the OAuth app lacks the scope, or the token expired |
| Jobber: sync says success but nothing in Jobber | should be impossible — `userErrors` is checked. If it happens, that check regressed |
| Webhook receiver sees invalid signatures | sign `f"{timestamp}.{raw_body}"`, using the **raw** body, not a re-serialized copy |
| `decrypt` fails after a key change | the writing key was removed from `CRM_ENCRYPTION_KEYS` before rows were re-encrypted. Put it back, re-encrypt, then drop it |
| Syncs stuck in `PROCESSING` | a worker died. The reaper recovers them after `CRM_STUCK_SYNC_MINUTES` |

Useful queries:

```sql
-- What is failing, and why
SELECT provider, status, last_error_code, count(*)
FROM crm_syncs WHERE tenant_id = :tenant
GROUP BY 1, 2, 3;

-- Which integrations still use a retired key
SELECT tenant_id, provider FROM crm_integrations
WHERE credentials_key_id = '2026a';

-- Backlog age
SELECT min(created_at), count(*) FROM crm_syncs WHERE status = 'PENDING';
```
