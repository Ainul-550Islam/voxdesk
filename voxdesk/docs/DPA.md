# Data Processing Agreement — template

> This is a **template** for the DPA you sign with tenants/buyers. Replace the
> bracketed items and have counsel review before use.

## 1. Parties

[Provider] (the **Processor**) provides the VoxDesk voice-agent platform to
[Customer] (the **Controller**), which determines the purposes of processing.

## 2. Subject matter, nature, purpose

Processing of call audio, transcripts and lead data to provide automated call
handling, appointment scheduling, CRM sync, and analytics, per the service
agreement.

## 3. Categories of data and data subjects

End-callers (patients/customers) and the Controller's leads: phone numbers,
call recordings, transcripts, appointment details.

## 4. Processing activities

- Inbound/outbound voice calls and SMS (Twilio).
- Speech-to-text (Deepgram), LLM processing (OpenAI/Anthropic/Google), TTS
  (ElevenLabs).
- Storage of calls, transcripts and leads in the Processor's database.

## 5. Processor obligations

- Process only on documented instructions.
- Confidentiality: personnel under confidentiality obligations.
- Security: implement the technical and organisational measures in
  `docs/SOC2.md` (encryption at rest/in transit, access control, audit logging).
- Sub-processors: listed in section 8; prior written consent for changes.
- Assist the Controller with data-subject requests via the export/erasure
  endpoints (`app/api/gdpr_routes.py`).
- Assist with breach notification per `docs/INCIDENT-RESPONSE.md`.
- Delete or return data on termination, subject to legal retention.

## 6. Retention

Recordings and transcripts are deleted after `CALL_RETENTION_DAYS` (default
365, configurable) — see `app/core/retention.py`.

## 7. Security measures

See `docs/SOC2.md` for the control matrix.

## 8. Sub-processors

Twilio (telephony), Deepgram (STT), ElevenLabs (TTS), OpenAI / Anthropic /
Google (LLM), Stripe (payments), Sentry (error reporting), [hosting provider].

## 9. Governing law & liability

[To be completed with counsel.]
