# VoxDesk — Voice providers & speech settings

How the live voice pipeline maps tenant voice settings onto the pinned
providers (Deepgram STT, ElevenLabs TTS, and the three LLM providers).

## Provider responsibilities

| Layer | Provider | Where configured |
|---|---|---|
| STT (speech-to-text) | Deepgram (`nova-3`, per-language fallback) | `app/agent/stt.py:build_stt` (model/language via `app/core/i18n.py:deepgram_options`) |
| LLM | OpenAI / Anthropic / Google, per-tenant preset | `app/agent/llm_factory.py` |
| TTS (text-to-speech) | ElevenLabs (streaming) | `app/agent/tts.py` |

## Provider error taxonomy

Every voice-provider failure is raised as a `ProviderError` (a
`RuntimeError` subclass in `app/agent/errors.py`) with a closed set of
categories and a `retryable` flag:

| Category | Retryable | Meaning |
|---|---|---|
| `configuration_error` | no | missing key / empty URL / bad setting |
| `authentication_error` | no | provider rejected our credentials (401) |
| `authorization_error` | no | credentials valid, request not permitted (403) |
| `rate_limit` | yes | 429, optional `retry_after` delay |
| `timeout` | yes | a bounded time budget was exceeded |
| `unavailable` | yes | network failure / 5xx |
| `invalid_request` | no | we sent something malformed |
| `unsupported_feature` | no | we asked for something the provider/SDK cannot express |
| `provider_error` | no | anything else |

Only transient categories are retryable; invalid credentials, invalid
requests and unsupported features are permanent and are never retried.
Every message is fixed safe text (no keys, no payloads, no customer data);
optional `detail` carries provider context and is **never** rendered by
`str()` or `safe_message`, so it cannot leak into logs.

## Speech speed (`Tenant.speech_speed`)

**Implemented and tested.** The pipeline maps each tenant's speech speed onto
ElevenLabs' `voice_settings.speed` field:

- **Supported range: 0.7–1.2** (ElevenLabs' streaming/agents range; the REST
  API is wider at 0.25–4.0 but the voice pipeline streams). Default **1.0**
  (normal pace), unchanged for every existing tenant.
- **Default is wire-identical to before:** when the speed is 1.0 the `speed`
  key is omitted from the websocket handshake, so default tenants send exactly
  the payload they always sent.
- **Out-of-range writes are rejected** with a 422 at the API boundary
  (`PATCH /api/tenants/{id}/voice`, `POST` tenant creation) — the range is
  shared with the pipeline via `app/agent/voice_settings.py`.
- **Out-of-range values persisted by older clients are clamped** (never
  rewritten in the database) at the provider boundary and reported as a
  structured warning (`tts.speech_speed_normalized`), so a provider never
  receives an unsupported value and the operator can see why.

### Why a subclass of the ElevenLabs service

The pinned `pipecat-ai==0.0.55` `ElevenLabsTTSService.InputParams` has no
`speed` field, so passing `speed=` there is silently dropped by pydantic.
ElevenLabs' own `voice_settings` object does support `speed`, and pipecat
forwards its `voice_settings` dict verbatim in the websocket handshake.
`SpeedAwareElevenLabsTTSService` (in `app/agent/tts.py`) adds `speed` at
exactly that layer. No provider field is passed that the provider cannot
express.

## Provider capability model

`app/agent/tts.py:ELEVENLABS_CAPABILITIES` records what the current
integration can and cannot map, so an unsupported setting is never forwarded
silently:

| Capability | Supported |
|---|---|
| `supports_speed` | yes (0.7–1.2) |
| `supports_stability` | yes (0–1) |
| `supports_similarity_boost` | yes (0–1) |
| `supports_style` | yes (0–1) |
| `supports_use_speaker_boost` | yes |
| `supports_language` | yes (multilingual models only) |
| `supports_streaming` | yes |
| `supports_pitch` | **no** — not exposed by pipecat 0.0.55 |
| `supports_interruptions` | **no** — no per-utterance interruption control |

`pitch` and other unsupported settings are intentionally absent from the
tenant model; they are not accepted, dropped, or faked.

`app/agent/stt.py:DEEPGRAM_CAPABILITIES` records the equivalent contract for
the STT layer (`supports_language`, `supports_model_selection`,
`supports_interim_results`, `supports_punctuation`, `supports_filler_words`,
`supports_streaming`).

`app/agent/llm_factory.py:LLM_CAPABILITIES` records the same contract per LLM
provider (`supports_tool_calling`, `supports_streaming`,
`supports_interruptions`). Tool-calling is the one capability the pipeline
*enforces*: the agent always registers its booking/escalation tools, so a
provider that cannot tool-call fails with `unsupported_feature` instead of
silently dropping the tools and producing an agent that cannot do its job.
An unknown provider has no capability entry, and "absent" is treated as
"cannot do it" — never as "can do it".

## STT model plumbing (fixed in Step 3)

The pinned `pipecat-ai==0.0.55` `DeepgramSTTService` has no `model`
constructor parameter — the model travels inside `live_options` — and it
requires `live_options` to be a `LiveOptions` object (it calls
`live_options.to_dict()`). The old pipeline passed a plain dict and a `model=`
keyword, so the service crashed with `AttributeError` on the first live call
and the nova-3/nova-2 language fallback never actually reached Deepgram.
`app/agent/stt.py:build_stt` now constructs the service correctly: the
resolved model (`nova-3` for covered languages, `nova-2` otherwise), language,
encoding, punctuation, interim results and filler words all travel inside a
proper `LiveOptions` instance.

## Fail-fast configuration validation

Each builder validates its own configuration **before** any network activity,
so a misconfigured deployment fails fast with a typed error instead of
starting a call that can never work:

- `build_stt` / `build_tts` raise `configuration_error` when the Deepgram /
  ElevenLabs key is missing.
- `build_llm` raises `unsupported_feature` for an unknown provider name
  (never a silent fallback to an unrelated AI) and falls back across the
  configured providers only when the requested provider has no key; it raises
  `configuration_error` when **no** provider has a key.

These errors propagate out of `run_voice_agent` to the media-stream handler,
which finalises the call `FAILED` through the idempotent `call_state` machine
and records the failure category, provider and retryability on the
`call.crashed` log line — without logging any secret.

## Observability

Provider failures are counted by the existing Prometheus setup (no second
metric system): `voxdesk_provider_errors_total{provider,category}`, defined
in `app/core/metrics.py` and incremented through
`app/agent/provider_observability.py`. Both labels are normalised to a fixed,
closed set before they touch the counter, so a misbehaving integration (or a
tenant-supplied string) cannot mint unbounded label series — the same
bounded-cardinality discipline as the HTTP path labels.

## Pipeline failure behaviour

- **Startup failure** (missing/invalid provider config, unsupported provider):
  the builder raises a typed `ProviderError` before any network activity;
  `run_voice_agent` propagates it; the media-stream handler marks the call
  `FAILED` via the idempotent `call_state` machine and logs the failure
  category/provider/retryability. No call is left stuck, no resources are
  left running, and finalisation is exactly-once.
- **Runtime failure** (pipeline-level crash, provider exception mid-call):
  `run_voice_agent` logs it and re-raises so the media-stream handler can
  finalise the call accurately. Turn persistence runs in `finally` as
  best-effort cleanup and never masks the call's primary outcome.
- **Normal hangup / transfer:** unchanged — a stream teardown caused by an
  `escalate_to_human` transfer is a successful handoff, not a failure, and an
  already-terminal call cannot be regressed (see `tests/test_stream_lifecycle.py`).
- **Stuck handshake:** the media stream accepts a socket and then waits for
  Twilio's `connected` + `start` frames under a bound
  (`STREAM_HANDSHAKE_TIMEOUT_SECONDS`, default 15, `0` disables). A client
  that connects and never speaks is closed with a 1008 policy-violation code
  instead of holding a database session and a pipeline slot open forever. The
  bound applies to the handshake only — it never touches the live
  conversation, which is driven by the websocket rather than a wall clock.
- **LLM:** `build_llm` falls back across providers when a key is missing and
  raises a clear error when none is configured (no key is ever logged).
- **STT/TTS:** services are constructed without network; provider
  authentication/connection failures surface as `authentication_error` /
  `unavailable` / `timeout` categories, and the API key is sent only in the
  websocket handshake, never logged.
- **Tenant settings** are normalised at the provider boundary as described
  above, so an invalid stored value cannot break the call path.

## What is tested vs. what still needs external validation

**Tested in CI** (`tests/test_tts.py`, deterministic, no network):

- speed normalization across and beyond the range,
- the exact `voice_settings` payload for default/fast/slow/out-of-range
  tenants (asserted against the installed pipecat 0.0.55 SDK),
- that `InputParams` still has no `speed` field (the contract that forced the
  subclass),
- that the 1.0 default keeps the pre-change wire payload,
- that out-of-range values are clamped **and** logged with no secret in the
  log record,
- API-boundary rejection of out-of-range writes (`tests/test_agent_config.py`),
- frontend input bounds (`dashboard/tests/agent.test.jsx`),
- provider-lifecycle hardening (`tests/test_provider_lifecycle.py`): the
  closed error taxonomy and retryability policy, secret isolation, fail-fast
  config validation for all three builders, the STT model/language resolution
  (nova-3 vs nova-2) and the `LiveOptions` regression, per-provider LLM
  construction, and the media-stream handler's accurate + exactly-once
  finalisation on a startup provider failure.

**Still requires real external-provider validation** (no live ElevenLabs
account is exercised here):

- that the ElevenLabs streaming endpoint honours `voice_settings.speed`
  (documented field, forwarded verbatim — not yet confirmed against a live
  account), and
- audible confirmation that a given speed multiplier produces the expected
  pace for the chosen voice/model.

## Configuration

- `Tenant.speech_speed` — `Float`, default `1.0`, validated 0.7–1.2 at the API.
- No database migration was required: the column, its default, and existing
  rows are unchanged. Normalization happens at the provider boundary only.
