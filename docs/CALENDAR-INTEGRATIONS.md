# Calendar integrations & scheduling

VoxDesk books appointments into whatever calendar a business already runs. This
document is the contract: what the layer guarantees, how to configure it, and
where the edges are.

> **Live verification status.** No calendar provider was contacted with real
> credentials while building this. See [§12](#12-what-was-not-live-tested) for
> exactly what that means and what was verified instead.

---

## 1. Architecture

```
  caller: "next Tuesday at three"
        │
        ▼
  nlp.parse_request        deterministic date/time -- never the LLM
        │
        ▼
  timezones.resolve_local  wall clock -> instant, DST-aware
        │
        ▼
  policy                   business hours, breaks, holidays, buffers, notice
        │
        ▼
  provider free/busy       network -- and a failure is NEVER emptiness
        │
        ▼
  VoxDesk appointments     our own commitments
        │
        ▼
  service.book             slot lock in the database, THEN the provider
        │
        ▼
  provider confirmation    CONFIRMED requires an external event id
        │
        ├──▶ CRM event      via STEP 5's abstraction
        └──▶ reminder       idempotent, replaced on reschedule
```

The load-bearing decisions, each of which exists because the audit
([`CALENDAR-AUDIT.md`](CALENDAR-AUDIT.md)) found the opposite:

**A booking is never claimed unless the provider accepted it.** `_confirm()` is
the only path that writes `CONFIRMED`, and it requires an `external_event_id`.

**An unreachable calendar is not an empty calendar.** `get_busy()` raises. The
old client returned `[]` on any exception, so an outage looked like total
availability.

**The slot lock is a database constraint, not application logic.** Two callers
racing compute the same `slot_key`; `UNIQUE (tenant_id, slot_key)` decides.

**An ambiguous timeout is reconciled, never blind-retried.**

### Files

| Path | Responsibility |
|---|---|
| `calendar/timezones.py` | UTC storage, DST, ambiguous/nonexistent times |
| `calendar/models.py` | `AvailabilitySlot`, `EventRequest`, `CalendarEvent`, `BookingResult`, slot/idempotency keys |
| `calendar/errors.py` | Normalized errors + HTTP classification |
| `calendar/base.py` | `CalendarProvider`, capabilities, the single HTTP entry point |
| `calendar/policy.py` | Business hours, slot grid, buffers, filters |
| `calendar/nlp.py` | Natural date/time interpretation |
| `calendar/service.py` | Orchestration: availability, book, reschedule, cancel |
| `calendar/tools.py` | Voice-agent tools |
| `calendar/registry.py` | Provider type → adapter class |
| `calendar/providers/` | One module per vendor |
| `api/appointment_routes.py` | Booking + integration + policy API |
| `api/calendar_webhook_routes.py` | Inbound provider notifications |

### Adding a provider

1. Write `providers/foo.py` subclassing `CalendarProvider`, declaring `name`,
   `capabilities`, and overriding only what you support.
2. Add `FOO` to `CalendarProviderType` **and to migration `0007`'s enum list**
   (the parity test enforces this).
3. Register it in `registry.py`.
4. Add credential/config allowlists in `api/appointment_routes.py`.

The contract tests in `tests/test_calendar_contract.py` are parameterized over
the registry, so the new adapter is immediately tested for tenant scoping,
error normalization, timeout handling, idempotency and secret handling.

---

## 2. Providers and capabilities

| Capability | Google | Microsoft | Cal.com | Internal | GSA¹ |
|---|:--:|:--:|:--:|:--:|:--:|
| `get_availability` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `free_busy` | ✅ | ✅ | —² | —³ | ⚠️ |
| `create_event` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `update_event` | ✅ | ✅ | —⁴ | ✅ | — |
| `cancel_event` | ✅ | ✅ | ✅ | ✅ | — |
| `get_event` | ✅ | ✅ | ✅ | — | — |
| `reschedule` | ✅ | ✅ | ✅⁵ | ✅ | — |
| `conferencing` | — | ✅ | ✅ | — | — |
| `health_check` | ✅ | ✅ | ✅ | ✅ | ✅ |

¹ `google_service_account` — the pre-STEP-6 path, kept for migration.
² Cal.com exposes bookable *slots*, not busy blocks. Declaring `free_busy`
would be inventing an operation.
³ No external calendar exists; the service checks VoxDesk's own appointments
separately, so declaring it would double-count.
⁴ Cal.com's PATCH does not move a booking's time.
⁵ A real dedicated endpoint, not emulated by an update.
⚠️ Works, but the legacy client cannot distinguish an outage from an empty
calendar. See §2.5.

Read this at runtime from `GET /api/calendar/providers` rather than hard-coding
it — a dashboard should grey out what a provider cannot do.

### 2.1 Google Calendar

* Base `https://www.googleapis.com/calendar/v3`, `Authorization: Bearer <token>`
* Free/busy `POST /freeBusy`; events `POST|GET|PATCH|DELETE /calendars/{id}/events`
* Refresh `POST https://oauth2.googleapis.com/token`, **form-encoded**

**The idempotency mechanism is a client-supplied event id.** Google lets the
caller set `id` and returns **409** if it exists. We derive it from the
booking's idempotency key, so a retry after a timeout either creates the event
or gets a 409 — and a 409 on *our own* key means "already booked", not
"someone else took it". Ids are base32hex (`a-v`, `0-9`, 5–1024 chars);
`_event_id()` filters accordingly.

Two quirks handled explicitly: a **403 may be a throttle**
(`rateLimitExceeded`), which is re-classified as retryable; and **410 Gone** on
a cancel is success, not an error.

Google does **not** resend `refresh_token` on refresh — the existing one is
carried forward. Dropping it is how an integration works for an hour and then
dies permanently.

Setup:

```
1. Google Cloud Console -> APIs & Services -> enable Google Calendar API
2. OAuth consent screen -> add scope https://www.googleapis.com/auth/calendar
3. Credentials -> OAuth client ID (Web application)
   Authorised redirect URI: https://your-host/api/calendar/oauth/google/callback
4. Run the authorization flow, capture access_token + refresh_token
5. PUT /api/calendar/integrations/google  (see §9)
```

### 2.2 Microsoft Outlook / Graph

* Base `https://graph.microsoft.com/v1.0`
* Free/busy `POST /me/calendar/getSchedule`; events `POST /me/events`, etc.
* Refresh `POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`,
  **form-encoded**

**The idempotency mechanism is `transactionId`** — Graph's documented
client-supplied dedupe value on create-event.

**The timezone decision, which is the trap in this API.** Graph historically
speaks *Windows* timezone names — `"Pacific Standard Time"`, not
`"America/Los_Angeles"`. Shipping an IANA↔Windows mapping table would be a
second source of truth for DST and a permanent maintenance liability. So this
adapter sends **UTC instants with `timeZone: "UTC"`** and asks for UTC back via
`Prefer: outlook.timezone="UTC"`. All local rendering stays in `timezones.py`,
where there is exactly one implementation of it.

Two more details: `dateTime` must be **naive** when `timeZone` is also present
(Graph rejects a body carrying both an offset and a zone); and Graph sends
**seven** fractional-second digits, which are truncated to six before parsing.

`scheduleItems` is omitted when the caller lacks detail permission, so the
adapter falls back to decoding `availabilityView` — otherwise a restricted
mailbox would look completely free.

Cancel tries `POST /events/{id}/cancel` first (notifies the attendee) and falls
back to `DELETE`, because Graph rejects `/cancel` for a non-organiser.

Setup:

```
1. Entra ID -> App registrations -> New registration
2. API permissions -> Microsoft Graph -> Calendars.ReadWrite,
   Calendars.Read.Shared, offline_access
3. Certificates & secrets -> New client secret
4. Redirect URI: https://your-host/api/calendar/oauth/microsoft/callback
5. PUT /api/calendar/integrations/microsoft
```

Microsoft **does** rotate the refresh token — the new one must be stored.

### 2.3 Cal.com

* Base `https://api.cal.com/v2`, `Authorization: Bearer cal_live_...`,
  `cal-api-version: 2024-08-13`
* `GET /slots`, `POST /bookings`, `POST /bookings/{uid}/reschedule`,
  `POST /bookings/{uid}/cancel`, `GET /bookings/{uid}`
* Responses are wrapped: `{"status": "success", "data": {...}}`

**Cal.com is a booking system, not a calendar**, which drives the capability
set. It has no free/busy and no arbitrary event creation — everything is a
booking against an `event_type_id`.

**A `status` other than success at HTTP 200 is a rejection.** Same trap as
Jobber's `userErrors` in the CRM layer: a refusal wearing a 200. And a taken
slot comes back as **400, not 409**, so that message is re-classified as a
conflict — otherwise it would be permanent and unhelpful.

Idempotency travels in `metadata.voxdesk_key`; there is no server-side dedupe,
so the database slot lock is the primary defence for this provider.

Setup:

```
1. cal.com -> Settings -> Developer -> API keys -> create (never expires)
2. Note the event type id from the event type's URL
3. PUT /api/calendar/integrations/calcom with config.event_type_id
4. Optional: Settings -> Webhooks -> add
   https://your-host/api/calendar/webhooks/calcom/<routing-token>
   with a secret, stored as credentials.webhook_secret
```

### 2.4 Internal

No external calendar. VoxDesk's `appointments` table is the entire source of
truth. This is the correct default for a business that has not connected
anything, and it makes the booking flow work end to end without a vendor
account. Event ids are deterministic from the idempotency key, so a retry after
a crash behaves exactly like a real provider's.

### 2.5 Google service account (legacy)

The pre-STEP-6 path, wrapping `app/integrations/google_calendar.py` unchanged.
Declares only `free_busy` and `create_event`, because those are the only two
methods the legacy client has.

**Known limitation, inherited and unfixable from outside:** the legacy client
catches every exception and returns `[]` or `None`, so this adapter cannot tell
an outage from an empty calendar. `create_event` returning `None` is at least
turned into a raise — that single change is what stops the old
"told the caller booked, nothing on the calendar" failure. Tenants should be
migrated to per-tenant OAuth (`google`), which does not have this problem.

---

## 3. Timezone rules

**Store UTC. Reason in the tenant's zone. Never let the two blur.**

| Rule | Where |
|---|---|
| Every tenant has an IANA timezone | `Tenant.timezone`, validated on write |
| Every appointment stores the zone it was agreed in | `Appointment.timezone` |
| All timestamps are UTC in the database | `DateTime(timezone=True)` |
| A naive datetime cannot reach an adapter | asserted in `models.__post_init__` |
| "Tomorrow" is the *business's* tomorrow | `nlp.parse_request(now=...)` in tenant zone |

`Appointment.timezone` is not derivable from the tenant after the fact: a
business that relocates, or corrects a wrong timezone, would otherwise silently
reinterpret every appointment already in the book. The offset alone is not
enough either — it does not survive a DST boundary.

### DST

**Nonexistent times** (clocks forward). `2026-03-08 02:30` does not exist in
New York. `zoneinfo` does not raise — it silently produces a datetime that
round-trips to a *different* wall clock, which is worse than an error. Detected
by round-tripping; the real transition boundary is found by binary search so
the clarification question can say "we skip from 2 to 3".

**Ambiguous times** (clocks back). `2026-11-01 01:30` happens twice. Both
candidates are offered rather than one being picked silently.

**Date arithmetic.** `moment + timedelta(days=1)` adds 24 hours of *elapsed*
time, which across a spring-forward boundary lands an hour off the wall clock.
"Same time tomorrow" means the wall clock, so `add_days_local()` adds the day
to the local calendar and re-resolves.

Half-hour and 45-minute zones work (`Asia/Kolkata`, `Australia/Lord_Howe`);
nothing assumes a whole-hour shift.

---

## 4. Business hours

Business policy is **not** provider availability. A dentist's Google calendar
being empty at 3 AM does not make 3 AM bookable. Both are checked, cheap local
filter first.

```json
{
  "weekly_hours": {
    "mon": [["09:00", "12:00"], ["13:00", "17:00"]],
    "sat": [],
    "sun": []
  },
  "holidays": ["2026-12-25", "2027-01-01"],
  "blocked_periods": [
    {"start": "2026-07-01T00:00:00", "end": "2026-07-14T23:59:59"}
  ]
}
```

A list of intervals per weekday, not one open/close pair, because **a lunch
break is the single most common reason a booking lands when nobody is there**.
An empty list means closed. `blocked_periods` are local wall-clock, resolved on
read so a vacation does not shift by an hour across a DST boundary.

An appointment must fit **entirely inside one interval**. Spanning the break is
not "mostly open", it is a customer in an empty waiting room.

Overlapping intervals are **rejected** on write rather than merged: it usually
means someone meant to write a break and got a boundary wrong, and silently
merging produces a schedule the tenant cannot see is wrong.

A tenant with no `SchedulingPolicy` row falls back to the pre-STEP-6
`Tenant.business_open` / `business_close` / `appointment_minutes` columns, so
nothing changes for anyone who has not opted in — with one deliberate
exception: `minimum_notice_minutes` now defaults to 60. The old code offered a
slot starting one second in the future, which is not a booking, it is a
surprise.

---

## 5. Buffers, notice and horizon

| Setting | Default | What it does |
|---|--:|---|
| `slot_minutes` | 30 | Appointment length |
| `slot_interval_minutes` | 30 | How far apart candidate starts are |
| `buffer_before_minutes` | 0 | Protected time before |
| `buffer_after_minutes` | 0 | Protected time after |
| `minimum_notice_minutes` | 60 | Soonest a caller may book |
| `booking_horizon_days` | 60 | Furthest ahead |
| `max_slots_offered` | 3 | How many the agent reads aloud |
| `allow_outside_business_hours` | false | Escape hatch |
| `require_provider_confirmation` | true | Refuse to book if the provider is unreachable |

Duration and interval are separate: a 60-minute appointment offered on a
30-minute grid gives twice the choice. The pre-STEP-6 code used one number for
both.

Slots are half-open intervals, so 09:00–09:30 and 09:30–10:00 do **not**
collide. Closed comparison would make every consecutive slot conflict with its
neighbour and silently halve capacity.

---

## 6. Booking, concurrency and idempotency

The order of operations in `service.book()` is the design:

1. **Idempotency lookup** — has this exact request already produced an
   appointment? Return it. First, so a retry never reaches the policy checks.
2. **Policy** — hours, notice, horizon. Local, no network.
3. **Reserve the slot** — a PENDING row whose unique `slot_key` is the lock.
4. **Call the provider** — only the winner gets here.
5. **Confirm** — external id and `CONFIRMED` written together.

Steps 3 and 4 in that order matter. Reserving *after* the provider call would
mean both racing callers create events and one then fails to persist, leaving
an orphan on the business's calendar.

The reservation uses a **SAVEPOINT**, not a bare flush: losing the race must
undo the reservation, not the caller's whole transaction.

### The keys

```
slot_key        = sha256(tenant | start_utc | end_utc)[:48]
idempotency_key = sha256(tenant | phone-or-email-or-request_id | start_utc)[:48]
```

Both derived, never generated. A fresh UUID per retry is not duplicate
protection — it is the opposite, because every retry then looks like a new
booking. `slot_key` is tenant-salted, so two businesses can hold the same hour.

Cancelling sets `slot_key = NULL`, releasing the time. NULLs are distinct in a
UNIQUE index in both PostgreSQL and SQLite, which is also what lets pre-STEP-6
rows coexist after the migration.

### The ambiguous timeout

The provider accepted the event and then the response timed out. A blind retry
creates a second calendar entry. So:

```
create_event -> CalendarTimeout
             -> find_event_by_key(key, window)
                ├─ found    -> treat as success, record its id
                ├─ absent   -> re-raise; the booking failed
                └─ ITSELF FAILED -> re-raise the original timeout
```

That last branch is the important one. **"I could not check" is not evidence of
absence.** The only safe reading of unknown is to leave the appointment
unconfirmed for a human rather than risk a duplicate.

`internal` and `google_service_account` cannot reconcile — the first because
nothing crosses a network, the second because the legacy client cannot read an
event back. For the latter, an ambiguous timeout ends as `FAILED`, which is
safe.

---

## 7. Rescheduling and cancellation

**Reschedule.** The row is not touched until the provider has accepted. If the
provider update fails, the appointment is left exactly as it was and the caller
is told their original time still stands. Cal.com's dedicated reschedule
endpoint is preferred over cancel-then-rebook, where a failure between the two
would leave the customer with nothing.

**Cancel.** Idempotent: cancelling an already-cancelled appointment returns
immediately without touching the provider. A second cancel is a 404 on Google
and a 400 on Cal.com, and surfacing those would make a harmless double-click
look broken.

If the *provider* cancel fails, the local state still moves to `CANCELLED`. The
customer said cancel; refusing to record that because a vendor API was slow
would keep the slot blocked and turn away the next caller. The error is kept in
`last_error` for staff to reconcile.

Cancelling also deletes any unsent reminder — a cancelled appointment must not
still text the customer.

---

## 8. Voice tools

```
check_availability(when, part_of_day)
book_appointment(customer_name, customer_phone, when, at, reason, customer_email)
reschedule_appointment(customer_phone, when, at, reason)
cancel_appointment(customer_phone, reason)
confirm_appointment(customer_phone)
```

`when` is **free text** on purpose — the caller's own words. Asking the model
for an ISO date is what the old code did, and it is exactly the "trust the
model for DST mathematics" mistake the brief forbids.

Three structural mechanisms stop the LLM inventing success:

1. **The outcome vocabulary is a closed enum.** There is no value the model can
   receive that means "booked" unless the service wrote it after a provider
   acknowledgement.
2. **The message text is written by the service**, not the model.
3. **`as_tool_payload()` is an allowlist** — outcome, ok, message, appointment
   id, slots. No provider name, no status code, no error string. The caller
   cannot be told "Google returned 401" because the model never learns it.

Outcomes: `AVAILABLE_SLOTS` `NO_SLOTS` `BOOKED` `ALREADY_BOOKED` `CONFLICT`
`RESCHEDULED` `CANCELLED` `CONFIRMED` `NOT_FOUND` `OUTSIDE_HOURS` `TOO_SOON`
`TOO_FAR` `NEEDS_CLARIFICATION` `FAILED`.

Every tool is bounded by `CALENDAR_VOICE_TIMEOUT_SECONDS` (default 3s), tighter
than the provider timeout: the provider may take six seconds, but the agent
cannot leave a caller in silence that long.

### Ambiguity

`"Tuesday"` said on a Tuesday, or `"at seven"` with no meridiem, produce
`NEEDS_CLARIFICATION` and a question. Hours that only make sense one way are
resolved (nobody books a dentist at 5 AM), but 7 and 12 are a coin flip and get
asked about. Spoken numbers work — `"half past four"`, `"quarter to five"` —
because speech-to-text produces words, not digits.

---

## 9. API

```
GET    /api/appointments/availability?day=YYYY-MM-DD&part_of_day=morning
GET    /api/appointments?status=confirmed&upcoming=true
POST   /api/appointments
GET    /api/appointments/{id}
PATCH  /api/appointments/{id}                       reschedule
POST   /api/appointments/{id}/cancel
POST   /api/appointments/{id}/no-show

GET    /api/calendar/providers
GET    /api/calendar/integrations
PUT    /api/calendar/integrations/{provider}
DELETE /api/calendar/integrations/{provider}
POST   /api/calendar/integrations/{provider}/test
GET    /api/calendar/policy
PUT    /api/calendar/policy
```

Permissions: `APPOINTMENT_READ` / `APPOINTMENT_WRITE` for bookings,
`INTEGRATION_READ` / `INTEGRATION_WRITE` for calendar connections. There is not
one role-string comparison in these files, and a test asserts it.

`starts_at_local` is a **local wall clock in the tenant's timezone**,
deliberately not UTC: a dashboard user picks a time on a clock, and making the
client convert is where offsets get dropped. A nonexistent or ambiguous local
time is a `422` with an explanation, never a silent shift.

```bash
curl -X POST https://api.example.com/api/appointments \
  -H "Authorization: Bearer $VOXDESK_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_name": "Jane Doe",
    "customer_phone": "+15550001111",
    "starts_at_local": "2026-09-15T15:00:00",
    "reason": "Cleaning",
    "request_id": "booking-attempt-1"
  }'
```

```bash
curl -X PUT https://api.example.com/api/calendar/integrations/google \
  -H "Authorization: Bearer $VOXDESK_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": {
      "access_token": "ya29.FAKE-for-docs",
      "refresh_token": "1//FAKE-for-docs",
      "client_id": "FAKE.apps.googleusercontent.com",
      "client_secret": "FAKE-client-secret"
    },
    "config": {"calendar_id": "primary"},
    "is_primary": true
  }'
```

*All credentials above are fake.*

Status mapping: `409` conflict, `422` policy refusal or unparseable time,
`404` not found (including another tenant's id), `502` provider failure.

---

## 10. Credential security

Calendar OAuth tokens use **STEP 5's cipher, key ring and rotation story** —
AES-256-GCM with `(tenant_id, provider)` as associated data, so ciphertext
copied from one tenant's row into another's fails to authenticate rather than
yielding a working token.

Never returned by any API, never logged, never in a JWT, never in an audit
`detail` (only the *names* of the fields supplied), never in a
`CalendarContext` repr. A 401 response body is excluded from error messages
entirely rather than merely scrubbed.

`CRM_ENCRYPTION_KEYS` is the same required production secret; connecting a
calendar without it returns `503` rather than falling back to plaintext.

---

## 11. Webhooks

```
POST /api/calendar/webhooks/{provider}/{routing_token}
```

The tenant is identified by a high-entropy path token derived per integration
from the platform secret. **The body is never consulted for routing** — a
`tenant_id` field is discarded, and there is a test for it.

The token is not the authentication; the provider's signature is. Anyone who
learns the token still cannot forge an event, which matters because URLs leak
into logs and screenshots.

| Provider | Verified by |
|---|---|
| Google | `X-Goog-Channel-Token` (a secret *we* supply at watch creation) |
| Microsoft | `clientState` (supplied at subscription creation); `validationToken` echoed |
| Cal.com | HMAC-SHA256 over the raw body |

Providers with no stored `webhook_secret` **fail closed**. Replay protection is
a `CalendarWebhookReceipt` per `(tenant, provider, provider_event_id)`; a
second delivery returns `200 {"duplicate": true}` — not a `409`, because a
provider that sees an error just retries.

**The only mutation performed is marking a locally-known appointment
cancelled** when the provider says its event is gone. That is where the
provider is unambiguously authoritative. Everything else an inbound
notification might imply is bidirectional sync and out of scope — acting on a
half-understood notification is how a webhook endpoint silently rewrites a
customer's diary.

---

## 12. What was not live-tested

**No calendar provider was contacted with real credentials.** Every adapter is
written against the published API contract and exercised against a scripted
HTTP transport that intercepts `httpx` — so header assembly, payload shape,
status classification and timeout handling are real code paths, but no byte
left the machine.

Not verified live:

* **Google Calendar** — no account, no OAuth client, no token. The
  client-supplied-event-id 409 behaviour, the `freeBusy` per-calendar `errors`
  shape and the refresh-token semantics all come from documentation.
* **Microsoft Graph** — no tenant, no app registration. The `getSchedule`
  response shape, `availabilityView` encoding, `transactionId` dedupe and
  `/cancel` vs `DELETE` behaviour come from documentation.
* **Cal.com** — no account, no API key. The v2 response envelope, the
  400-means-conflict behaviour and the several slot shapes come from
  documentation and community reports.
* **Inbound webhooks** from any real provider.
* **The full OAuth authorization flow** — token *refresh* is implemented and
  unit-tested; the initial authorization-code exchange and its callback route
  are **not implemented** (see §14).
* **PostgreSQL.** All tests run on SQLite. `alembic upgrade head` has not been
  executed; migration/model parity is checked by static AST parsing.

What *was* verified: request construction and payload mapping for all five
providers, response normalization, error mapping, retry classification,
idempotency including the timeout-after-acceptance case, genuine concurrent
booking on separate database connections, DST arithmetic across four zones,
tenant isolation, credential encryption, and the full API. 440 tests.

---

## 13. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `503 "encryption is not configured"` | `CRM_ENCRYPTION_KEYS` unset |
| Every booking returns `TOO_SOON` | `minimum_notice_minutes` too high, or the client is sending UTC in `starts_at_local` |
| `OUTSIDE_HOURS` for a time that looks open | the appointment spans a break; it must fit entirely inside one interval |
| Availability empty and `degraded` non-null | the provider could not be reached — this is the honest answer, not "no slots" |
| Bookings an hour out twice a year | something is doing date arithmetic in UTC; use `add_days_local` |
| `FAILED` with no calendar entry | correct behaviour when `require_provider_confirmation` is on and the provider refused |
| `PENDING` appointments accumulating | `require_provider_confirmation` is off and the provider is failing; check `last_error` |
| Google: errors blaming the payload | usually a missing scope or the wrong `calendar_id`; the adapter always sends the auth header |
| Google: `invalid_grant` on refresh | the user revoked access. Permanent — the tenant must reconnect |
| Microsoft: times an hour out in Outlook | expected — the adapter sends UTC, and Outlook renders in the *user's* zone |
| Microsoft: `getSchedule` returns nothing | no `mailbox` or `schedule_id` configured |
| Cal.com: "no longer available" | genuine conflict; Cal.com reports it as a 400, and the adapter re-classifies |
| Cal.com: `event_type_id` errors | every booking is against an event type; it is required config |
| Webhook signatures always invalid | sign the **raw** body, not a re-serialized copy |
| Two appointments in one slot | should be impossible — `uq_appointment_slot` is the guarantee. If it happens, that constraint is missing from the database |

Useful queries:

```sql
-- Appointments that never reached a provider
SELECT id, starts_at, last_error FROM appointments
WHERE tenant_id = :tenant AND status IN ('PENDING', 'FAILED');

-- Which tenants are still on the shared service account
SELECT tenant_id FROM calendar_integrations
WHERE provider = 'GOOGLE_SERVICE_ACCOUNT';

-- Tokens about to expire
SELECT tenant_id, provider, token_expires_at FROM calendar_integrations
WHERE token_expires_at < now() + interval '1 day';
```

---

## 14. Limitations

1. **No OAuth authorization-code flow.** Token *refresh* is implemented;
   obtaining the first token pair is not. Credentials are supplied through the
   API today, which works for a self-hosted operator but is not a
   customer-facing "Connect Google" button.
2. **No automatic token-refresh sweep.** `refresh_access_token()` exists and is
   tested, but nothing calls it on a schedule yet; `token_expires_at` is
   stored ready for that.
3. **Google push notifications carry no payload** — they say "this calendar
   changed, go and look". Acting on that needs a sync query, so those
   notifications are recorded and nothing is mutated.
4. **Inbound is one-way** — verify, deduplicate, and cancel. Bidirectional sync
   is out of scope.
5. **`google_service_account` cannot cancel or reschedule** (§2.5).
6. **Reminders are queued, not redesigned.** STEP 6 fixed the timezone bug and
   added dedupe and cancellation; the dispatch channel remains the existing
   SMS path.
7. **No recurring appointments.**
8. **Availability is computed per request** with no caching. Fine at current
   scale; a busy tenant polling availability will hit the provider each time.
