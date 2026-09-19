# VoxDesk — Cost sources & the ESTIMATED-vs-AUTHORITATIVE rule (Step 10, item M)

## How VoxDesk prices usage

`app/billing/cost.py` holds **no prices**. Cost is computed from the
operator-supplied `COST_UNIT_PRICES` JSON (validated in
`app/core/config.py`), expressed in **millicents per natural unit**
(1/100,000 of a dollar). A missing or zero price records **UNKNOWN**: the
volume is still counted, the dollars are not, and nothing is invented. Revenue
(what tenants pay) is a separate number owned by the plan catalogue and is
never conflated with cost.

Key vocabulary (anything else is ignored, so a typo degrades to UNKNOWN):

| Key | Natural unit | Example |
|---|---|---|
| `voice_minute` | 1 minute of Twilio voice | `1300` = $0.0130/min |
| `sms_segment` | 1 SMS segment | `790` = $0.0079/segment |
| `llm_1k_tokens` | 1,000 tokens (any provider) | `15` = $0.00015/1k tokens |
| `tts_1k_chars` | 1,000 characters (ElevenLabs) | `30` = $0.00030/1k chars |

Provider-specific LLM keys are supported (`llm_1k_tokens_openai`,
`llm_1k_tokens_anthropic`, `llm_1k_tokens_google`) via
`app/billing/cost.py`.

## Where to obtain AUTHORITATIVE prices

**None of the figures below are hardcoded and none are asserted as current.**
An operator must populate `COST_UNIT_PRICES` from the provider's live pricing
page at deploy time; anything read from a pricing page is AUTHORITATIVE as of
the read date, anything remembered or copied from docs is ESTIMATED until
verified.

| Item | Source (verify at deploy time) |
|---|---|
| Twilio voice / SMS | https://www.twilio.com/pricing/voice (and /pricing/messaging) — per-number/per-country rate card |
| ElevenLabs TTS | https://elevenlabs.io/pricing — per-character rate for the chosen model (`eleven_flash_v2*` etc.) |
| OpenAI LLM | https://openai.com/api/pricing — per-1k-token input/output for the tenant's model (`gpt-4o-mini` preset) |
| Anthropic LLM | https://www.anthropic.com/pricing — per-1k-token for `claude-haiku-4-5` / `claude-sonnet-4-5` |
| Google Gemini | https://ai.google.dev/gemini-api/docs/pricing — per-1k-token for `gemini-2.0-flash` |

## Status of the price table in this repository

* **`COST_UNIT_PRICES` default: empty** → every resource prices as UNKNOWN
  until an operator sets it. This is deliberate and documented, never a bug.
* The `tests/` billing suite asserts the UNKNOWN path and the idempotency
  boundary, not any specific dollar value.
* `docs/SOC2.md` and the launch checklist list "populate provider pricing" as
  an operator step; it is **NOT_RUN** in this sandbox (no real pricing fetch
  was performed, and no figure in this file is claimed authoritative).
