# Real-provider integration validation (Step 4)

How VoxDesk validates the **real external providers** it depends on — safely,
deterministically, and only when a human explicitly asks for it.

This is **not** a replacement for the unit and mocked-integration tests. Those
already run on every push and prove the application's *behaviour*. This layer
proves the *outside world*: that the credentials in the environment actually
work against the live Twilio, Deepgram, ElevenLabs, LLM, calendar, CRM and
Stripe APIs.

> **A passing real-provider health check does NOT prove the complete
> production voice/call lifecycle works.** That still requires end-to-end
> validation (a real call end to end), which is a separate, manual step. See
> "What is forbidden" below.

## The three test levels

| Level | Name | Network | Purpose |
|---|---|---|---|
| 1 | Unit | none | fast, deterministic logic tests |
| 2 | Mocked integration | none (fake responses) | provider contracts and app behaviour against realistic fixtures |
| 3 | Real provider | real, opt-in | the credentials in the environment actually work |

Levels 1 and 2 are the existing `tests/` suite. Level 3 is the new machinery in
`app/integrations/validation/`, exercised by `tests/test_real_providers.py`
(marker `real_provider`) and the `scripts/validate_providers.py` command.

## Enabling real integrations

Everything is gated by one flag:

```bash
VOXDESK_REAL_INTEGRATION=1
```

Without it, no network call is made. With it:

```bash
# The environment validation command (table output):
VOXDESK_REAL_INTEGRATION=1 python -m app.integrations.validation.cli
# or:
VOXDESK_REAL_INTEGRATION=1 python scripts/validate_providers.py

# Machine-readable output:
VOXDESK_REAL_INTEGRATION=1 python -m app.integrations.validation.cli --json

# One provider only:
VOXDESK_REAL_INTEGRATION=1 python -m app.integrations.validation.cli --provider Twilio

# The pytest layer:
VOXDESK_REAL_INTEGRATION=1 python -m pytest tests/test_real_providers.py -q
```

There is deliberately **no second flag** and **no `--force` switch**: the
environment variable is the single, documented switch.

## Required environment variables

**Voice stack** (these are existing `Settings` fields, so they are also read
from `.env`):

| Provider | Variable(s) |
|---|---|
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| Deepgram | `DEEPGRAM_API_KEY` |
| ElevenLabs | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` |
| OpenAI | `OPENAI_API_KEY` (model: `VOXDESK_REAL_OPENAI_MODEL`, default preset) |
| Anthropic | `ANTHROPIC_API_KEY` (model: `VOXDESK_REAL_ANTHROPIC_MODEL`) |
| Google LLM | `GOOGLE_API_KEY` (model: `VOXDESK_REAL_GOOGLE_MODEL`) |
| Stripe | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` |

**Tenant-scoped providers** (credentials are normally stored per tenant,
encrypted, in the database; the harness reads one synthetic connection from
these variables):

| Provider | Variable(s) |
|---|---|
| Google Calendar | `VOXDESK_REAL_GOOGLE_CALENDAR_REFRESH_TOKEN`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_CALENDAR_ID` (default `primary`), optional `_ACCESS_TOKEN` |
| Microsoft Calendar | `VOXDESK_REAL_MICROSOFT_REFRESH_TOKEN`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_CALENDAR_ID`, optional `_ACCESS_TOKEN` |
| Cal.com | `VOXDESK_REAL_CALCOM_API_KEY`, optional `VOXDESK_REAL_CALCOM_BASE_URL` |
| HubSpot | `VOXDESK_REAL_HUBSPOT_TOKEN` |
| GoHighLevel | `VOXDESK_REAL_GHL_ACCESS_TOKEN`, `VOXDESK_REAL_GHL_LOCATION_ID` |
| Jobber | `VOXDESK_REAL_JOBBER_ACCESS_TOKEN` |

A missing variable makes that provider report **SKIPPED**, never a fake PASS.

## Safety classification

Every real operation is classified into exactly one of three buckets:

| Class | Meaning | Automated? |
|---|---|---|
| `READ_ONLY` | credential verification, listings, availability lookup, account fetch | yes (opt-in) |
| `SAFE_SANDBOX_WRITE` | creating a test-only object in a dedicated sandbox account when unavoidable | no — documented only |
| `FORBIDDEN_IN_AUTOMATED_TEST` | real charge, SMS, phone call, production booking, customer-record change, customer email | **never** |

The registry enforces this structurally: every runnable check is `READ_ONLY`,
and the forbidden operations are a documentation list with **no runnable check
behind them**, so an automated run cannot perform one even by accident.

## Provider-by-provider validation scope

| Provider | What is checked (all read-only) | What is NOT done |
|---|---|---|
| Twilio | account fetch via the pinned SDK | no call, no SMS |
| Deepgram | projects list (`GET /v1/projects`) | no audio upload |
| ElevenLabs | voices list, configured voice exists, configured model exists | no audio generation |
| OpenAI | configured model exists (`GET /v1/models/{model}`) | no completion call |
| Anthropic | models list, configured model present | no completion call |
| Google LLM | models list, configured model present | no generation call |
| Google Calendar | adapter `health_check` (read-only calendar fetch; token refresh if only a refresh token is supplied) | no event created |
| Microsoft Calendar | adapter `health_check` (read-only) | no event created |
| Cal.com | event-type listing | no booking created |
| HubSpot | contact listing (`limit=1`) | no contact created/updated |
| GoHighLevel | location-scoped contact listing | no contact created/updated |
| Jobber | account GraphQL query | no record changed |
| Stripe | `GET /prices` read-only; webhook secret shape (`whsec_`) | **no charge, ever** |

For the tenant-scoped providers the harness reuses the **existing adapter
`health_check()`** methods — the same probes the dashboard's "Test connection"
button runs — so the framework cannot drift from the real integration path.

## Failure classification

| Status | Meaning |
|---|---|
| `SKIPPED` | required credentials or the opt-in flag are absent |
| `PASS` | the provider successfully validated |
| `FAIL` | a configuration or provider error (bad key, model missing, 5xx, timeout) |
| `BLOCKED` | the environment prevents execution |

`BLOCKED` is never reported as `PASS`.

## Expected output

```
Provider              Status    Detail
Twilio                PASS      account authenticated (Acme Voice)
Deepgram              SKIPPED   DEEPGRAM_API_KEY not configured
...
Stripe                PASS      read-only validation (test key); no charge or subscription created
```

The command also prints a summary count, the list of never-automated
operations, and the note that live end-to-end voice validation is manual.

## Secret handling

* Checks read credentials **only** from the environment / `Settings`. Nothing
  is committed, snapshotted, or written to disk.
* Checks emit fixed, safe text: no URLs (a URL can carry a key in its query
  string), no response bodies (a 401 body can reflect a key), no raw exception
  strings (an httpx error can embed a URL).
* The reporter additionally runs every string through a `SecretMasker` built
  from every credential the checks read, so even a buggy check that
  interpolated a token cannot print it.
* The CI workflow passes secrets via `secrets.*`; they never appear in logs or
  in the repository.

## CI treatment

* **Ordinary CI** (`ci.yml`) runs `pytest -m "not real_provider"`, so the
  network suite is excluded structurally even before its autouse skip fires.
  Ordinary CI is deterministic, network-independent and credential-free.
* **Real integrations** run only through the manual workflow
  `.github/workflows/real-integrations.yml` (`workflow_dispatch`). A human
  triggers it from the Actions tab; missing secrets simply make the affected
  providers SKIP.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| everything `SKIPPED`, message "VOXDESK_REAL_INTEGRATION not set" | the flag is missing |
| `SKIPPED ... not configured (missing: ...)` | that provider's variables are unset |
| `FAIL credentials rejected (HTTP 401/403)` | the key/token is wrong or revoked |
| `FAIL model '...' not available` | the configured model does not exist on that account |
| `FAIL timed out` | the provider is unreachable from this network |
| `FAIL ... (HTTP 5xx)` | provider-side outage — retry later |

## What must be completed before end-to-end validation

This framework validates *connectivity and configuration*. Proving the full
production voice/call lifecycle (real Twilio media stream → Deepgram →
LLM → ElevenLabs → booking/transfer → billing) needs a manual, human-triggered
end-to-end test against an operator-controlled phone number, because it
requires placing a real phone call — a `FORBIDDEN_IN_AUTOMATED_TEST` operation
by design.
