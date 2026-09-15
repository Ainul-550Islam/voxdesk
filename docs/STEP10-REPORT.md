# VoxDesk — Step 10: Release-candidate hardening report

**Date:** 2026-09-12 · **Branch:** `step9/scale-compliance` (Step 9 commit `4f0cffb`,
this step builds on top of it) · **Scope:** dependency hardening, provider
compatibility revalidation, staging/deployment readiness, launch gates.

Every claim in this report is labelled with its status. Nothing here is
claimed as **operationally verified** unless it was actually executed in this
sandbox; the sandbox has no Docker, no Postgres, no rclone and no real
provider credentials, so items K, N, O and the live-call E2E (L) are **static
validation with an explicit blocker** — never a claimed PASS.

---

## A. Dependency baseline & matrix

The full current→target matrix. "CURRENT" is the Step 9 pin (verified with
`importlib.metadata`), "TARGET" is the Step 10 pin in `requirements.txt`.
Every upgrade has an explicit reason (security advisory, or a compatibility
requirement forced by the pipecat 0.0.94 upgrade). "COMPATIBILITY TEST" is the
deterministic test that proves the new version works; "RESULT" is the observed
outcome in the Step 10 gate run.

### A.1 Upgraded packages

| Package | Current | Target | Reason | Risk | Compatibility test | Result |
|---|---|---|---|---|---|---|
| pipecat-ai | 0.0.55 | **0.0.94** | Item B (highest priority); fixes framework advisories, keeps voice stack current | Medium (live voice path) | `tests/test_imports.py`, `test_tts.py`, `test_provider_lifecycle.py` + full suite | **PASS** (2254) |
| fastapi | 0.115.6 | **0.136.1** | Item F: gates the Starlette advisory fixes; 0.136.1 is the newest release that still flattens `app.routes` (0.137.0+ lazy `_IncludedRouter` breaks Step 9 route-contract tests) | Low | full API/middleware/WebSocket/security-header/TrustedHost/CORS/auth suites | **PASS** (2254) |
| starlette | 0.41.3 | **1.6.0** | Item F: closes 7 advisories (multipart DoS, Range DoS, BadHost, StaticFiles UNC-path SSRF, HTTP-method dispatch, form() limits, authority poisoning); inside FastAPI 0.136.1's `starlette>=0.46.0` | Low–Med | TrustedHost/CORS/auth/security-header/WebSocket suites | **PASS** (2254) |
| cryptography | 44.0.1 | **50.0.1** | Item G: closes 7 advisories (OpenSSL-in-wheel 48.0.1, subgroup 46.0.5, DNS-name-constraint 46.0.6, wildcard-DNS verifier 49.0.0, duplicate intermediates 49.0.0, PKCS#7 Bleichenbacher 50.0.0) | Low (app uses only `AESGCM`) | `tests/test_crm_credentials.py` + `test_security_regression.py` (round-trip, rotation, cross-tenant AAD, tamper, failure paths) | **PASS** (41/41) |
| pydantic | 2.10.5 | **2.10.6** | Item B: pipecat 0.0.94 base requires `pydantic>=2.10.6,<3`; patch bump | Low | full suite | **PASS** |
| openai | 1.59.6 | **1.74.0** | Item B: pipecat 0.0.94 base requires `openai>=1.74.0,<3`; no advisory on 1.59.6 | Low | `app/agent/text_agent.py` (`AsyncOpenAI`) + LLM tests | **PASS** |
| anthropic | 0.45.2 | **0.49.0** | Item B: pipecat 0.0.94 `anthropic` extra requires `anthropic~=0.49.0`; no advisory on 0.45.2 | Low | LLM provider tests | **PASS** |
| deepgram-sdk | 3.8.0 (implicit) | **4.7.0** (explicit) | Item B: `app/agent/stt.py` now imports `LiveOptions` from `deepgram` directly; pipecat 0.0.94 `deepgram` extra requires `~=4.7.0` | Low | STT tests (`test_provider_lifecycle.py`) | **PASS** |
| httpx | 0.27.2 | **0.28.1** | Item B: pipecat 0.0.94 `google` extra pulls `google-genai>=1.41.0` which requires `httpx>=0.28.1`; no advisory on 0.27.2 | Low | billing/calendar/CRM provider tests (httpx `AsyncClient`) | **PASS** |
| aiofiles | 25.1.0 | **24.1.0** | Item B: pipecat 0.0.94 base requires `aiofiles<25,>=24.1.0` | Low | dashboard StaticFiles serving tests | **PASS** |

New transitive dependencies introduced by pipecat 0.0.94 base (no app code
touches them): `loguru 0.7.3`, `Markdown 3.10.3`, `nltk 3.10.3`, `numba
0.61.2` (+`llvmlite 0.44.0`), `scipy 1.18.1`, `numpy 2.2.6`, `protobuf
5.29.6`, `pyloudnorm 0.1.1`, `resampy 0.4.3`, `soxr 0.5.0.post1`,
`audioop-lts 0.2.2`, `docstring_parser 0.18.0`, plus `google-genai 1.75.0`,
`google-cloud-speech 2.40.0`, `google-cloud-texttospeech 2.37.0` (google
extra) and `onnxruntime 1.30.0` (silero extra). All imported/loaded cleanly in
the fresh-venv install smoke test and the full suite.

### A.2 Verified clean — no change

OSV + `pip-audit` report **no advisories** at the current pins, so they were
left alone (the "do not upgrade unrelated packages" rule): `sqlalchemy[asyncio]
2.0.36`, `asyncpg 0.30.0`, `alembic 1.14.0`, `twilio 9.4.1`, `redis 5.2.1`,
`PyJWT 2.13.0`, `bcrypt 4.2.1`, `python-multipart 0.0.31`, `python-dotenv
1.2.2`, `pypdf 6.16.1`, `boto3 1.35.90`, `python-docx 1.2.0`, `structlog
24.4.0`, `sentry-sdk 2.19.2`, `prometheus-client 0.21.0`, `websockets 13.1`,
`uvicorn 0.34.0`, `email-validator 2.2.0`, `google-api-python-client
2.157.0`, `google-auth-oauthlib 1.2.1`, `pydantic-settings 2.7.0`,
`aiosqlite 0.20.0`, `ruff 0.8.4`, `pytest-asyncio 0.25.0`.

### A.3 Documented residuals (accepted, see `docs/SECURITY.md` §7)

| Package | Version | Finding | Classification |
|---|---|---|---|
| pillow | 11.3.0 | 35 advisories, all fixed only in 12.x; **pipecat 0.0.94 base pins `Pillow<12`** | P3 — not reachable: audio-only pipeline never decodes images |
| pytest | 8.3.4 | PYSEC-2026-1845 (tmpdir), fixed in 9.0.3 | P3 — dev-only tooling; major bump deferred |
| nltk | 3.10.3 | pip-audit flags it, but 3.10.3 **is** the OSV fixed version and the newest 3.x | P3 — boundary false positive |
| vitest | 3.2.7 | GHSA-82fw-gwwq-j7x9 (2 moderate, dev-only via `@vitest/mocker`) | P3 — dev-only; fix is a breaking vitest 4.x bump |

`scripts/audit_dependencies.sh` was updated: the accepted list is now
`["nltk","pillow","pytest"]` (the previously-accepted aiohttp, cryptography,
pipecat-ai and starlette are all clean now and were removed). The gate exits 0
with only these three documented.

---

## B. Pipecat 0.0.55 → 0.0.94 evaluation (highest priority)

Decision: **upgrade, with proof.** Every surface the app uses was checked
against the 0.0.94 wheel source before the bump. Findings:

* **Module layout.** Each provider is now a package whose `__init__` is a
  `DeprecatedModuleProxy`; canonical classes live one level deeper. The app
  was moved to the canonical paths so a future pipecat release that removes
  the proxies fails loudly at import, not on a live call:
  * `pipecat.services.elevenlabs.tts.ElevenLabsTTSService`
  * `pipecat.services.deepgram.stt.DeepgramSTTService` (+ `LiveOptions` now
    imported from the `deepgram` SDK — pipecat no longer defines it)
  * `pipecat.services.openai.llm.OpenAILLMService`
  * `pipecat.services.anthropic.llm.AnthropicLLMService`
  * `pipecat.services.google.llm.GoogleLLMService` (`google.google` is itself
    a deprecation shim in 0.0.94)
  * `pipecat.transports.websocket.fastapi` (the `transports.network.*` path is
    a deprecation re-export)
* **Constructors.** All five provider constructors take the exact keyword
  arguments the builders pass (`api_key`, `model`, `voice_id`, `sample_rate`,
  `live_options`, `params=...InputParams(...)`) with compatible defaults.
* **InputParams.** OpenAI: `frequency_penalty/presence_penalty` (−2..2),
  `temperature` (0–2), `max_tokens` (≥1) — all present. Anthropic: `max_tokens`
  (≥1), `temperature` (0–1). Google: `max_tokens` (≥1), `temperature` (0–2).
  ElevenLabs: **gained** `speed`, `language`, `style`, `use_speaker_boost`,
  `auto_mode`, `enable_ssml_parsing`, `enable_logging`,
  `apply_text_normalization`, `pronunciation_dictionary_locators`; stability
  and similarity are now independently optional (the 0.0.55 "must supply both"
  rule is gone).
* **ElevenLabs speech-speed seam.** The subclass overrides
  `_set_voice_settings()`, which 0.0.94 still calls from `__init__` and
  `_update_settings`. 0.0.55 forwarded `voice_settings` in the WS connect URL;
  0.0.94 forwards it in the per-context message
  (`{"text":" ","context_id":…,"voice_settings":{…}}`). Either way the
  `speed` the subclass injects reaches the provider. `speed=1.0` is still
  omitted for default tenants.
* **Deepgram.** `DeepgramSTTService(*, api_key, url, base_url, sample_rate,
  live_options, addons, **kw)` still calls `live_options.to_dict()`;
  `_settings` still carries `model`/`language`/`encoding`/`sample_rate`/
  `filler_words`. deepgram-sdk 4.7 `LiveOptions.endpointing` accepts `int`
  (250 ms), and all fields the builder sets exist.
* **Pipeline/transport/VAD.** `Pipeline`, `PipelineRunner(handle_sigint=)`,
  `PipelineTask(pipeline, params=)`, `PipelineParams(allow_interruptions,
  enable_metrics, enable_usage_metrics, audio_in/out_sample_rate)`,
  `OpenAILLMContext(messages, tools)`, `create_context_aggregator` →
  `OpenAIContextAggregatorPair` with `.user()/.assistant()/.get_context_frame()`,
  `FastAPIWebsocketTransport(websocket, params)`, `TwilioFrameSerializer(stream_sid,
  call_sid, account_sid, auth_token)`, `SileroVADAnalyzer(params=VADParams(...))`
  (model `.onnx` bundled in the wheel) — all present and signature-compatible.
* **Function calling.** `register_function(name, handler)` calls the handler
  with a single `FunctionCallParams` whose `function_name`, `arguments`,
  `result_callback` fields match `app/agent/pipeline.py`'s `_tool_bridge`.
* **Usage metrics.** `MetricsFrame`/`LLMUsageMetricsData`/`TTSUsageMetricsData`
  still exist; `enable_usage_metrics=True` still makes the OpenAI LLM emit
  `LLMUsageMetricsData(total_tokens)` and ElevenLabs TTS emit
  `TTSUsageMetricsData(characters)` downstream, so the Step 7 `UsageTracker`
  and billing metering keep working unchanged.

Tests updated for the new contracts (not deleted): `tests/test_imports.py`
(canonical paths + runtime surface), `tests/test_tts.py` (InputParams now has
`speed`; stability/similarity independent; wire-payload assertion unchanged).

---

## C. ElevenLabs TTS speech speed (revalidated)

Path: `tenant.speech_speed` → `build_tts` → `normalize_speech_speed` (clamps
to 0.7–1.2) → `SpeedAwareElevenLabsTTSService(speed=…)` → `_set_voice_settings()`
adds `voice_settings.speed` → provider. `tests/test_tts.py` (23 cases) covers
normal/min/max/out-of-range clamp + warning, in-range no-warning, secret-safety
of the warning, default omitted, model/voice resolution, and a real tenant row.
**Result: PASS under 0.0.94.** No change to the range policy (ElevenLabs WS
speed remains documented 0.7–1.2; 1.0 = provider default).

---

## D. LLM providers (revalidated)

`app/agent/llm_factory.py` constructs OpenAI/Anthropic/Google services with
`api_key`, `model`, and `InputParams(temperature, max_tokens, …)`; all three
0.0.94 constructors accept exactly this. Fallback, capability checks,
fail-fast missing-key, and secret-safe logs are unchanged and covered by
`tests/test_provider_lifecycle.py`. **No default model changed** (presets
`gpt-4o-mini` / `claude-haiku-4-5` / `gemini-2.0-flash` untouched — the 0.0.94
class defaults `gpt-4.1` / `claude-sonnet-4-5-20250929` / `gemini-2.5-flash`
never apply because the factory always passes an explicit model).

---

## E. Deepgram STT (revalidated)

Streaming model/language resolution (`nova-3` vs `nova-2`), interim results,
endpointing (250 ms), smart-format, filler-words (English only), mulaw/8 kHz,
and the `LiveOptions` (not dict) requirement are unchanged. Tests pin
`_settings["model"]`/`filler_words`/`encoding`/`sample_rate` and the
`LiveOptions` object contract. **Result: PASS under 0.0.94 + deepgram-sdk 4.7.0.**

---

## F. FastAPI / Starlette

`fastapi 0.115.6 → 0.136.1`, `starlette 0.41.3 → 1.6.0` (see matrix A.1).
Rationale for 0.136.1 over latest: FastAPI 0.137.0 introduced a lazy
`_IncludedRouter` route model that stops `app.routes` being flattened at
import, which would silently break the Step 9 route-introspection contract
tests; 0.136.1 is the newest release that still flattens routes **and** permits
`starlette>=0.46.0`. Starlette 1.6.0 keeps `TrustedHostMiddleware`,
`starlette.exceptions.HTTPException`, `datastructures.Headers` and
`middleware.cors` at the same import paths; `TrustedHostMiddleware` now also
rejects a **missing** Host header with 400, so the BadHost advisory is fixed at
both the framework and app layer. **Result: full suite PASS (2254).**

---

## G. Cryptography

`44.0.1 → 50.0.1`. VoxDesk uses only
`cryptography.hazmat.primitives.ciphers.aead.AESGCM`
(`app/integrations/crm/crypto.py`); that API is identical across 44→50, and the
envelope format `v1.<key_id>.<nonce>.<ct>` is app-defined, so **no stored
credential changes and no migration**. Re-ran the encryption/rotation/
cross-tenant-AAD/tamper/failure suite: `tests/test_crm_credentials.py` +
`tests/test_security_regression.py` — **41/41 PASS**.

---

## H. Regression matrix

| Gate | Result |
|---|---|
| Backend full suite (network-independent) | **2254 passed, 30 skipped, 13 deselected** (deselected = `real_provider` marker) |
| Frontend suite | **362 passed (14 files)** |
| Frontend production build | **OK** (272.04 kB JS / 11.03 kB CSS gzip) |
| `ruff check app tests scripts` | **All checks passed** |
| `compileall app tests scripts` | **OK** |
| Fresh-venv `pip install -r requirements.txt` | **OK** (no resolver conflicts) |
| pipecat import smoke (all 5 providers + transport/VAD/frames/metrics) | **OK** |
| bandit (`-r app -ll`) | **25 Low / 0 Medium / 0 High** (identical to Step 9) |
| `pip-audit -r requirements.txt` | 3 accepted residuals only (nltk/pillow/pytest); audit gate **exit 0** |
| `npm audit --omit=dev` | **0 vulnerabilities** |
| `npm audit` (incl. dev) | 2 moderate (vitest `@vitest/mocker`; dev-only) |

---

## I. Real-provider validation framework

The real-provider path is **opt-in only**: `real-integrations.yml` is
`workflow_dispatch` (no push/PR/schedule) and runs
`python -m app.integrations.validation.cli` + `pytest tests/test_real_providers.py`
with per-provider secrets, every one optional (missing → SKIPPED). CI
(`ci.yml`) stays network-independent: `pytest -m "not real_provider"`.

Compatibility check: the provider health checks in
`app/integrations/validation/voice.py` are plain **httpx GET** calls to REST
endpoints (Deepgram `api.deepgram.com/v1/projects`, ElevenLabs
`api.elevenlabs.io/v1/voices|models`, OpenAI `api.openai.com/v1/models`,
Anthropic `api.anthropic.com/v1/models`, Gemini
`generativelanguage.googleapis.com/v1beta/models`) — they do **not** import
deepgram-sdk, pipecat, or the LLM SDKs, so the SDK bumps do not affect them;
httpx 0.28.1 keeps the `AsyncClient(timeout=…)` calls identical. Twilio,
Google/Microsoft/Cal.com calendar, HubSpot/GHL/Jobber CRM, and Stripe health
checks use the same httpx/twilio-SDK paths and are unaffected. **No real
provider call was made in this step** (no credentials present).

---

## J. Egress network policy

See **`docs/STEP10-EGRESS-POLICY.md`** (allowed-domain inventory + deny rules +
enforcement guidance). App-layer SSRF guard (`app/core/ssrf.py`) is unchanged
and tested; the network-layer policy is an operational requirement that this
sandbox cannot apply (no Docker).

---

## K. Staging validation

**BLOCKED — not executed.** This sandbox has no Docker
(`command -v docker` → absent), no Postgres, no Redis. Static validation of the
compose/deploy/migrate/smoke/readiness/metrics surfaces was performed against
`scripts/` and `docker-compose*.yml`, but **no runtime staging validation is
claimed**. The exact blocker is the absence of a container runtime and
database in the execution environment.

---

## L. Real telephony E2E

**Not executed — no live call placed.** The complete, human-executed
pre-call / test-call / post-call checklist is in
**`docs/STEP10-E2E-RUNBOOK.md`**. The pipecat upgrade is validated by the
deterministic suite only; a live-call E2E is required before claiming voice
E2E PASS and remains open.

---

## M. Cost model

VoxDesk hardcodes **no prices**. `COST_UNIT_PRICES` (JSON, operator-supplied)
feeds `app/billing/cost.py`; a missing/zero price records UNKNOWN and never
invents a number. Key vocabulary: `voice_minute`, `sms_segment`,
`llm_1k_tokens`, `tts_1k_chars` (millicents/unit). See
**`docs/STEP10-COST-SOURCES.md`** for the per-provider reference URL and the
ESTIMATED-vs-AUTHORITATIVE rule.

---

## N. Off-site backup

**Static validation only.** `scripts/backup.sh` (pg_dump `-Fc`, `pg_restore
--list` integrity check, 14-day retention, `rclone copy` when `RCLONE_REMOTE`
is set) and `scripts/restore.sh` are present and well-formed. **Blocker:** this
sandbox has no `pg_dump`, no `rclone`, no `RCLONE_REMOTE` and no database, so
no connectivity or integrity run happened. Procedure: **`docs/STEP10-BACKUP-SYNC.md`**.

---

## O. Deployment drill

**Static validation only — no production/non-production deploy executed.**
`scripts/deploy.sh` (ff-only pull → build → pre-deploy backup w/ integrity
check → migrations under advisory lock → recreate → readiness ≤120s → verify;
never auto-downgrades the DB) and `scripts/rollback.sh`/`scripts/restore.sh`
are present. **Blocker:** no Docker/compose in this sandbox. The drill record
template + the never-blindly-downgrade rule: **`docs/STEP10-DEPLOY-DRILL.md`**.

---

## P. Pentest readiness

The Step 9 checklist (`docs/PENTEST-CHECKLIST.md`) remains the handover
artefact and was reviewed, not re-executed. No destructive testing was done
and **no external penetration test is claimed**. Staging accounts, log
collection, monitoring, backup retention and incident runbooks
(`docs/INCIDENT-RESPONSE.md`, `INCIDENT-TRIAGE.md`, `BCP.md`) are referenced
from the runbook; rate limits and the audit trail are implemented and tested.

---

## Q. Release gate

Single checklist. **PASS** = proven in this step's execution;
**NOT_RUN** = requires the operator/human (or infrastructure) and was not
executed; **BLOCKED** = requires infrastructure this sandbox lacks.

| Domain | Item | Status |
|---|---|---|
| CODE | `ruff` clean, `compileall` OK, imports OK | PASS |
| CODE | Backend suite 2254 + frontend 362 green | PASS |
| SECURITY | bandit 25 Low / 0 Med / 0 High | PASS |
| SECURITY | `pip-audit` gate exit 0 (3 documented residuals) | PASS |
| SECURITY | npm prod audit 0 vulns | PASS |
| SECURITY | Secret scan (Step 9 gitleaks/pattern) clean | PASS (Step 9) |
| SECURITY | Third-party penetration test | NOT_RUN (never claimed) |
| TESTS | Deterministic suite network-independent | PASS |
| DEPENDENCIES | All bumps compatibility-tested (matrix A) | PASS |
| DATABASE | Migrations under advisory lock scripted | PASS (static) |
| DATABASE | Real Postgres migration round-trip | NOT_RUN (no DB here) |
| INFRASTRUCTURE | Docker/compose build + smoke | BLOCKED (no Docker) |
| INFRASTRUCTURE | Egress network policy applied | BLOCKED (no network control plane) |
| OBSERVABILITY | Metrics/readiness/structured-log code paths | PASS (suite) |
| OBSERVABILITY | Staging dashboard/alert live validation | NOT_RUN |
| PROVIDERS | Health-check code paths compatible | PASS (static) |
| PROVIDERS | Real provider reachability | NOT_RUN (opt-in, no creds) |
| VOICE E2E | Real telephony call | NOT_RUN (checklist only) |
| BILLING | Metering/cost code paths | PASS (suite) |
| BILLING | Provider pricing populated | NOT_RUN (operator) |
| BACKUP/DR | Backup/restore scripts validated | PASS (static) |
| BACKUP/DR | Off-site sync + restore drill | BLOCKED (no rclone/DB) |
| COMPLIANCE | SOC 2 / HIPAA / PCI / ISO | NOT_APPLICABLE (not claimed) |
| PENTEST | Scoped external pentest | NOT_RUN (never claimed) |

---

## R. Blocker classification

| Priority | Issue | Detail |
|---|---|---|
| **P0** | none | — |
| **P1** | none | — |
| **P2** | Live-call E2E not yet run | pipecat 0.0.94 upgrade is suite-validated but a real telephony call must be executed before voice E2E can be claimed PASS (`docs/STEP10-E2E-RUNBOOK.md`). |
| **P2** | Egress network policy not applied | Static SSRF guard is in place; the container/network deny rules need the real infrastructure (no Docker here). |
| **P3** | `pillow 11.3.0` advisories | Blocked by pipecat `Pillow<12`; not reachable (no image decode path). |
| **P3** | `pytest 8.3.4` tmpdir advisory | Dev-only; fix is a pytest 9 + pytest-asyncio 1.x migration. |
| **P3** | `vitest 3.2.7` (2 moderate dev advisories) | Dev-only; fix is a breaking vitest 4.x bump. |
| **P3** | `nltk 3.10.3` pip-audit finding | False positive — 3.10.3 is the fixed version. |

---

## S. Tests added / updated in this step

* `tests/test_imports.py` — canonical pipecat 0.0.94 import paths + runtime
  surface (transport moved to `pipecat.transports.websocket.fastapi`).
* `tests/test_tts.py` — InputParams now **has** `speed`; stability/similarity
  independent; wire-payload assertion unchanged (renamed to reflect the
  0.0.94 context-message seam).
* No test was deleted. The full suite is the compatibility proof.

## T. Documentation inventory

| File | Covers |
|---|---|
| `docs/STEP10-REPORT.md` | this report (matrix, B–U narrative) |
| `docs/STEP10-E2E-RUNBOOK.md` | item L — final voice E2E checklist |
| `docs/STEP10-EGRESS-POLICY.md` | item J — allowed domains + deny rules |
| `docs/STEP10-COST-SOURCES.md` | item M — pricing sources + rules |
| `docs/STEP10-BACKUP-SYNC.md` | item N — backup/restore procedure |
| `docs/STEP10-DEPLOY-DRILL.md` | item O — deployment drill record |
| `docs/SECURITY.md` | §7/§8 updated (accepted risks post-Step 10) |
| `requirements.txt` | updated pins + reasons (comments) |

## U. Quality-gate command log (this step, executed)

```text
/home/user/.venv/bin/python -m pytest tests/ -q -p no:warnings -m "not real_provider"
    -> 2254 passed, 30 skipped, 13 deselected
/home/user/.venv/bin/ruff check app tests scripts          -> All checks passed
/home/user/.venv/bin/python -m compileall -q app tests scripts  -> OK
/home/user/.venv/bin/bandit -r app -ll                     -> 25 Low / 0 Med / 0 High
PATH=/home/user/.venv/bin bash scripts/audit_dependencies.sh -> OK (3 accepted)
(cd dashboard && npm run test)                             -> 362 passed
(cd dashboard && npm run build)                            -> OK (272.04 kB / 11.03 kB)
(cd dashboard && npm audit --omit=dev --audit-level=high)  -> 0 vulnerabilities
```
