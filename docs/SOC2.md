# VoxDesk — SOC 2 readiness (Type I scope)

VoxDesk is **SOC 2-ready**, not yet SOC 2-certified. This document is the
control matrix an auditor will walk; each control maps to code or an operating
procedure so the gap between "ready" and "certified" is evidence collection,
not engineering.

## Trust service criteria (TSC) map

| Criterion | Control | Where |
|---|---|---|
| **Security** | MFA-ready auth: bcrypt, JWT (15-min access, rotating refresh), lockout after `MAX_FAILED_LOGINS` | `app/auth/`, `app/core/config.py` |
| | RBAC with least privilege (permission enum, role hierarchy) | `app/auth/permissions.py`, `dependencies.py` |
| | Tenant isolation enforced in queries (never from request) | `app/auth/dependencies.py` + isolation tests |
| | Encryption at rest for CRM credentials (AES-256-GCM) | `app/integrations/crm/crypto.py` |
| | Encryption in transit (TLS) + HSTS | edge TLS, `app/core/security_headers.py` |
| | Secrets never logged/returned; fail-closed boot validation | `app/core/config.py:validate_security()` |
| | Security headers on all responses | `app/core/security_headers.py` |
| | Rate limiting (fail-closed) | `app/core/rate_limit.py` |
| | SAST + dependency + secret scanning in CI | `.github/workflows/security-scan.yml` |
| **Availability** | Health/readiness probes + container HEALTHCHECK | `app/main.py`, `Dockerfile` |
| | Postgres backup (nightly) + restore procedure | `docker-compose.prod.yml` (backup), `scripts/` |
| | Deployment with rollback runbook | `scripts/deploy.sh`, `docs/OPS.md` |
| | Monitoring + alerting (Prometheus/Grafana) | `observability/` |
| **Confidentiality** | Allowlist response models (no secret can leak) | billing/integration routes + tests |
| | Data retention policy + deletion | `app/core/retention.py`, `data_policy.py` |
| | Data-subject export/erasure | `app/api/gdpr_routes.py` |
| **Privacy** | Consent provenance recorded | `tenants.data_consent_*` columns |
| | Purpose limitation (transcripts not shared unless opted in) | `share_transcripts` defaults false |
| **Processing Integrity** | Idempotent webhooks (replay detection) | `crm_webhook_receipts`, `calendar_webhook_receipts` |
| | Reconciliation reports discrepancies (never silent) | `app/billing/reconciliation.py` |
| | Audit log of privileged actions | `AuditEvent`/`AuditLog` + `record_audit()` |

## What the auditor will still require (operating evidence, not code)

1. **Policies**: see `docs/INCIDENT-RESPONSE.md`, `docs/BCP.md`, `docs/DPA.md`.
2. **Access management**: documented onboarding/offboarding, least-privilege
   review cadence, and the "remove a leaver" runbook.
3. **Change management**: PR approval policy, the CI gate as evidence of
   testing, and a change log.
4. **Vendor management**: the list of sub-processors (Twilio, Deepgram,
   ElevenLabs, OpenAI/Anthropic/Google, Stripe, Sentry, your host).
5. **Penetration test**: annual external pentest report.
6. **Monitoring evidence**: N days of Grafana dashboards + alert responses.

## Control owners

- Engineering: code controls (this repository).
- Operator: policy execution, access reviews, vendor reviews, evidence capture.
