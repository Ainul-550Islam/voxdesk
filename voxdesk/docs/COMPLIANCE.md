# VoxDesk — Compliance map (TCPA · A2P 10DLC · EU AI Act · healthcare)

This document maps VoxDesk's existing behaviour to the regulations buyers ask
about, so a sales conversation or due-diligence questionnaire has one place to
point. Where a control exists in code, the file is named. Where an action is
the operator's responsibility, it is flagged **operator**.

---

## 1. TCPA (US telephone consumer protection)

| Requirement | Where it lives |
|---|---|
| No calls outside the tenant's calling window | `app/telephony/outbound.py:is_call_window_open()` — enforced per-tenant in the tenant's own timezone; out-of-window leads are scheduled (`next_window_start()`), not dropped into the window |
| Honour do-not-call | `app/telephony/outbound.py:next_callable_leads()` excludes DNC leads; `LeadStatus.DNC` set via `POST /api/tenants/{id}/leads/{id}/do-not-call` (`app/api/routes.py`) and the agent tool `mark_do_not_call` (`app/agent/functions.py`) |
| Retry discipline (no hammering) | `RETRY_BACKOFF_HOURS = (1, 4, 24)` with an attempt cap, `app/telephony/outbound.py` |
| Prior express consent | **operator** — captured at opt-in; the A2P registration checklist below demands the screenshot |

## 2. A2P 10DLC (SMS in the US)

Implemented in `app/core/compliance.py`:

- `check_message()` blocks SHAFT content (cannabis, loans, gambling, firearms, tobacco/vape) and public URL shorteners **before** sending
- `ensure_opt_out()` appends the required STOP footer; `is_sendable()` gates every outbound SMS
- `count_segments()` reports GSM-7 vs UCS-2 (Bengali is UCS-2 — important for BD/English campaigns)
- `registration_checklist()` is the client-facing paperwork list; `register_brand()` drives Twilio Trust Hub

Surfaced through `POST /api/compliance/check-message` and `GET /api/compliance/a2p-checklist` (permission `COMPLIANCE_READ`).

## 3. EU AI Act (transparency — Art. 50)

| Requirement | Where it lives |
|---|---|
| Callers must know they are speaking to an automated agent | `AI_DISCLOSURE_REQUIRED=true` (default), checked by `app/core/data_policy.ai_disclosure_present()` against the tenant's greeting; a greeting like "…this is Alex…" with no AI signal is flagged non-compliant |
| Records of the interaction | Call + Turn models (`app/db/models.py`) retain transcripts; audit trail in the `AuditEvent` table |

**Operator:** ensure each tenant's greeting names the assistant as automated
("…I'm Alex, an AI assistant…"). The disclosure check is the guardrail; the
greeting text is tenant content.

## 4. Healthcare / HIPAA-style expectations (dental vertical)

VoxDesk is **not** a covered entity and makes no HIPAA compliance claim — say
this plainly to buyers. What it does provide:

| Control | Where it lives |
|---|---|
| PHI-ish data encrypted at rest (CRM credentials) | AES-256-GCM, `app/integrations/crm/crypto.py`, required in production (`CRM_ENCRYPTION_KEYS`) |
| Secrets never logged/returned | `app/core/config.py:validate_security()` + billing/CRM secret handling |
| Tenant isolation (cross-tenant access is attacked in tests) | `tests/test_tenant_isolation.py`, `tests/test_crm_tenant_isolation.py`, `tests/test_knowledge_tenant_isolation.py` |
| Recording disclosure to callers | `Tenant.recording_disclaimer` (`app/db/models.py:216`) |
| Transcript sharing opt-in | `Tenant.share_transcripts` default `False` (`app/db/models.py:950`) |
| Full audit log | `AuditEvent` with `ix_audit_tenant_time` index (`app/db/models.py`) |
| Data retention | `CALL_RETENTION_DAYS` (default 365); enforced by `app/core/retention.py` (daily purge of `Call`/`Turn` rows) + policy in `app/core/data_policy.py` |

**Operator:** sign a BAA where a buyer requires it. The retention purge runs
automatically on the daily scheduler loop (see §5). Healthcare buyers will also
ask for a SOC 2 or penetration test — that is a services engagement, not a code
change.

## 5. Data retention & deletion

`app/core/data_policy.py` defines the policy (pure, unit-tested):

- `retention_cutoff(days)` → the timestamp boundary
- `is_expired(created_at, days)` → whether a record is past retention

`app/core/retention.py` **enforces** it: `purge_expired_calls()` deletes `Call`
rows and their dependent `Turn` rows older than `CALL_RETENTION_DAYS`, bounded
per run so a first pass over a large table cannot stall the worker. The
scheduler invokes it once a day (`retention_loop()` in `scripts/scheduler.py`,
interval `RETENTION_INTERVAL_SECONDS`). The purge is idempotent — repeated runs
delete only newly-expired rows — and is covered by `tests/test_retention.py`.

**Recordings:** VoxDesk does not copy recording media into its own storage.
`Call.recording_url` is a pointer to provider-hosted media (Twilio) and the
current pipeline never populates it, so there is no VoxDesk-side recording
object to expunge. Deleting media held in a provider account is a
provider-account operation. If an object-storage recording backend is ever
added, its deletion must be wired into `purge_expired_calls()` so recordings
cannot outlive the policy (documented in that module).

**Audit logs are deliberately not deleted** — SOC 2 requires the audit trail to
outlive the operational data it describes.

## 6. Buyer-ready one-liner

> VoxDesk enforces per-tenant calling windows, do-not-call and retry backoff
> (TCPA), blocks non-compliant SMS content and drives Twilio Trust Hub (A2P
> 10DLC), flags greetings that fail AI-agent disclosure (EU AI Act), and ships
> encryption-at-rest, tenant isolation, audit logging and a configurable
> retention policy for healthcare-style buyers.
