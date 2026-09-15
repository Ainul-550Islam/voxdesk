# VoxDesk — Security architecture, threat model & controls (Step 9)

This is the consolidated security reference for VoxDesk, written as the
deliverable of the **Step 9 deep security / privacy / abuse-prevention /
compliance-readiness / pentest-readiness audit**. It states what exists, where
it lives, what is tested, and — with equal honesty — what is *not* yet true
(no certification is claimed; no penetration test has been performed).

Every claim below carries a **status**, using one of five fixed labels:

| Label | Meaning |
|---|---|
| **IMPLEMENTED** | The control exists in code. |
| **TESTED** | A deterministic test in `tests/` proves the behaviour. |
| **OPERATIONALLY VERIFIED** | Proven in a running environment (e.g. real Postgres migration smoke test); not merely unit-tested. |
| **EXTERNALLY AUDITED** | A third party has assessed it. **None of VoxDesk's controls carry this label yet.** |
| **CERTIFIED** | A certification body has issued a certificate (SOC 2, HIPAA, PCI, ISO 27001, …). **Nothing in VoxDesk carries this label.** |

VoxDesk is **SOC 2-*ready*, not SOC 2-certified** (see `docs/SOC2.md`), and
the same distinction applies everywhere else. Statements of certification or
of an external penetration test are claims the project must never make until
they are actually true.

---

## 1. Threat model

**Actors.** (1) An anonymous internet client; (2) an authenticated user of one
tenant (any role, including a compromised account); (3) an external service
that sends webhooks (Twilio, Stripe, GoHighLevel, HubSpot, Jobber, Google,
Microsoft, Cal.com); (4) a caller on the phone; (5) an uploaded document that
carries a prompt-injection payload; (6) an operator with production access.

**Assets.** Customer call recordings and transcripts; lead contact data
(names, phone numbers, emails); business knowledge-base documents; CRM and
calendar provider credentials (access + refresh tokens); billing and
entitlement state; the audit trail; the ability to place outbound calls and
SMS.

**Crown jewels.** CRM/calendar credentials (they unlock the tenant's external
systems), transcripts (call content), and the audit trail (evidence).

**Trust boundaries.** Browser → Caddy (TLS terminates at the edge) → FastAPI
(loopback) → Postgres. Twilio/Stripe/providers → public webhook endpoints →
FastAPI. The media WebSocket stream is the only browser-independent channel
and is authenticated with a short-lived signed token.

**Top threats addressed.** Cross-tenant object access (IDOR); privilege
escalation; credential exfiltration from the DB or via SSRF; webhook replay or
forgery; prompt injection through documents or the caller; SMS/call abuse
(TCPA, A2P 10DLC); billing manipulation; secret leakage through APIs, logs, or
the dashboard.

---

## 2. Attack-surface inventory (item A)

| Surface | Entry points | AuthN | AuthZ / tenant guard |
|---|---|---|---|
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me`, `/auth/roles` | email+password / opaque refresh cookie | tenant from verified JWT |
| Registration | `POST /auth/register` (owner bootstrap) | — | creates tenant + owner |
| Admin/tenant | `PATCH /api/tenants/{id}/settings`, `/team/**`, `/leads/**`, `/appointments/**` | JWT + RBAC | `ctx.tenant_id` + `get_owned()` |
| Agent | `GET /api/agent/**` | JWT + RBAC | tenant-scoped |
| Billing | `POST /billing/checkout`, `/cancel`, `/portal`, `POST /api/billing/webhooks` | JWT / Stripe signature | tenant from JWT; webhook by signature |
| Analytics | `GET /api/analytics/**` | JWT + RBAC | every query `WHERE tenant_id` |
| Integrations | `POST/GET/PATCH/DELETE /api/integrations/**` (CRM + calendar) | JWT + RBAC | credentials/config allowlists |
| Knowledge | `POST /api/knowledge/documents`, `GET /documents`, `/search` | JWT + RBAC | tenant-mandatory retrieval |
| File upload | `POST /api/knowledge/documents` (multipart) | JWT + RBAC | size/type/ext/traversal checks |
| Webhooks (in) | `POST /api/integrations/crm/webhook/{token}`, `/calendar/webhook/{token}` | HMAC signature + routing token | token is tenant-bound, never body |
| Billing webhook | `POST /api/billing/webhooks` | Stripe raw-body signature | — |
| Telephony | `POST /telephony/voice`, `/status`, `/transfer`, `/media` (WS), `/outbound` | Twilio HMAC / signed stream token | per-call |
| Health/metrics | `GET /health`, `/ready`, `/metrics` | bearer token (metrics) | fail-closed on absence |
| Dashboard | static SPA under `/dashboard` | cookie session | API is the boundary |

---

## 3. Findings from the Step 9 audit

### 3.1 Confirmed and FIXED in this step

**F-1 — SSRF via tenant-configurable outbound URLs (Medium).** CRM
(`base_url` for GoHighLevel/HubSpot/Jobber; `url` for the webhook provider)
and calendar (`base_url`/`token_url` for Google/Microsoft/Cal.com) accepted a
destination that the application later contacted with the tenant's bearer
token (and, for `token_url`, the OAuth `client_secret` + `refresh_token` in
the POST body). Only the webhook provider's `url` was restricted to HTTPS, and
no provider blocked loopback/private/link-local/metadata targets.

**Fix:** new `app/core/ssrf.py` (`validate_outbound_url`) classifies a URL by
its literal components (never by resolving DNS) and rejects non-http(s)
schemes, embedded userinfo, loopback, RFC 1918 / RFC 4193, link-local,
reserved, multicast, and metadata addresses (`169.254.169.254`), plus
`localhost`/`*.localhost`/`.local`/`.internal`/metadata hostnames. Enforced at
two layers: (a) the API boundary in `_validated_config`
(`app/api/integration_routes.py`, `app/api/appointment_routes.py`), where
tenant input arrives; and (b) the provider `_base()`/`token_url` accessors
(`app/integrations/crm/providers/*`, `app/integrations/calendar/providers/*`),
so a value written by any other path is also refused at use. The calendar
`token_url` and CRM `base_url` also require HTTPS because credentials travel
to them. **Status: TESTED** (`tests/test_ssrf.py`, 21 cases).

**F-2 — Tool dispatch resolved arbitrary attribute names (Medium).**
`FunctionHandlers.dispatch(name, args)` resolved the tool name through
`getattr(self, name)` with no allowlist. A prompt-injected model emitting
`__class__`, `dispatch`, `_scheduling`, `tenant`, … would reach an attribute
rather than a tool.

**Fix:** `DISPATCHABLE_TOOLS` allowlist in `app/agent/functions.py`; anything
outside it returns `{"ok": false, "message": "Unknown function …"}` before any
attribute lookup. **Status: TESTED** (`tests/test_security_regression.py`).

**F-3 — No per-call ceiling on tool invocations in the live voice path
(Low/Medium).** The text path caps tool rounds (`MAX_TOOL_ROUNDS = 4`), but
the voice `_tool_bridge` had no cap, so a looped or injected model could run
unbounded paid operations.

**Fix:** `MAX_TOOL_CALLS_PER_CALL = 12` in `app/agent/pipeline.py`; the bridge
refuses further tools once exceeded. **Status: IMPLEMENTED**
(the voice path itself is not unit-testable without a live call; the guard is
deterministic and isolated).

**F-4 — Password denylist was only 12 entries (Low).** `app/auth/password.py`
`_COMMON` was a stub.

**Fix:** expanded to a ~60-entry breach-corpus head list covering entries that
*also* satisfy the 12-character / 3-of-4-class gates (so they would otherwise
be accepted). **Status: TESTED** (`tests/test_auth_login.py`).

**F-5 — Host-header validation was absent at the application layer (Low).**
The reverse proxy terminates TLS for the configured domains, but the API
accepted any `Host` header.

**Fix:** `TRUSTED_HOSTS` setting (comma-separated, empty = off so the
single-proxy topology is unchanged) + conditional `TrustedHostMiddleware` in
`app/main.py`. **Status: TESTED** (`tests/test_security_regression.py`).

**F-6 — Dependency advisories.** `pip-audit` found vulnerable pins. Fixed
this step (all verified by the full backend suite):

| Package | From | To | Rationale |
|---|---|---|---|
| `PyJWT` | 2.10.1 | 2.13.0 | GHSA-752w-5fwx-jx9f (alg confusion) + DoS advisories, same major |
| `cryptography` | 44.0.0 | 44.0.1 | CVE-2024-12797 (RSA PKCS#1 v1.5 padding oracle), patch-only |
| `python-multipart` | 0.0.20 | 0.0.31 | CVE-2026-24486 + five further multipart DoS advisories, same 0.0.x |
| `python-dotenv` | 1.0.1 | 1.2.2 | PYSEC-2026-2270, same major |
| `pypdf` | 5.1.0 | 6.16.1 | 41 advisories; PDF is an untrusted-upload parser; extractor uses only the stable `PdfReader` API; full suite green |

**Status: TESTED** (2250 tests) for the bumps; the remaining advisories are
documented as **accepted risks** in §7.

### 3.2 Confirmed and already strong (no change needed)

* **JWT/refresh/auth** (`app/auth/jwt.py`, `service.py`, `dependencies.py`):
  HS256 with pinned decode and required `iss`/`aud`/`exp`/`iat`/`typ=access`;
  `tv` (token version) re-checked against the DB on every request; opaque
  48-byte refresh tokens stored only as SHA-256 digests, single-use rotation,
  reuse detection revokes all sessions.
* **Password policy** (`app/auth/password.py`): bcrypt cost 12, 72-byte
  rejection, NFKC normalisation, length + 3-of-4-class rules, dummy-hash
  timing equalisation, login lockout after `MAX_FAILED_LOGINS`.
* **RBAC** (`app/auth/permissions.py`, `rbac.py`): pure-data permission map,
  no lambda injection; `get_owned()` returns 404 (not 403) for cross-tenant
  reads; tenant comes only from the verified JWT.
* **Encryption at rest** (`app/integrations/crm/crypto.py`): AES-256-GCM
  envelope `v1.<key_id>.<nonce>.<ct>`, random 96-bit nonce, AAD bound to
  `(tenant_id, provider)`, key-ring rotation (active + readable keys), fail-
  closed 503 when `CRM_ENCRYPTION_KEYS` is absent. Calendar credentials use
  the same module.
* **Inbound webhooks** (`app/api/crm_webhook_routes.py`,
  `calendar_webhook_routes.py`, `app/billing/webhooks.py`): body `tenant_id`
  is never trusted (routing token is HMAC-derived); signatures compared in
  constant time; raw body used for Stripe signature with a freshness window;
  per-provider event receipts (DB unique constraints) make replays idempotent;
  bodies capped at 1 MB.
* **File upload** (`app/api/knowledge_routes.py`, `app/knowledge/ingest.py`,
  `extractors/`, `storage/`): hard size cap enforced mid-read (`limit + 1`
  then 413); magic-bytes-first detection with declared MIME as fallback only;
  `DANGEROUS_EXTENSIONS` refuse list; tenant-mandatory dedup; storage keys
  `tenant/<tenant_id>/<document_id>/<filename>`; `safe_filename` +
  `_resolve` prevent traversal; atomic temp-file writes.
* **RAG isolation** (`app/knowledge/vectorstore.py`, `retrieval.py`,
  `context.py`): the tenant predicate is part of the SQL `WHERE`, never a
  post-filter; retrieval signatures require `tenant_id`; results never expose
  scores/vectors/storage keys; excerpts are fenced and injection-shaped lines
  are neutralised; the system prompt forbids acting on document instructions.
* **Billing** (`app/api/billing_routes.py`, `app/billing/*`): request models
  accept only plan code/interval (no price, tenant, customer IDs); responses
  are allowlists; entitlement changes only via verified Stripe webhook;
  receipts honour provider ordering; manual provider has no real charge path.
* **Telephony** (`app/telephony/*`): media WS requires a 120 s signed stream
  token bound to `call_sid`; Twilio HTTP webhooks are HMAC-verified (dev skip
  flag is startup-gated); the E2E guard arms only outside production and
  refuses ordinary traffic when armed; outbound respects call windows, DNC,
  attempt caps and per-campaign `calls_per_minute`.
* **Observability/hardening** (`app/main.py`, `app/core/*`): `X-Request-ID`
  sanitised; production hides Swagger/ReDoc/OpenAPI and refuses insecure
  config at boot; CORS origins from explicit settings; dashboard mount has a
  traversal check; the SPA fallback never returns HTML for API/telephony/auth
  paths; structured logging redacts known keys/prefixes and configured secret
  values.

### 3.3 Documented gaps (accepted, not fixed in code)

* **Password reset, email verification, MFA and OAuth login do not exist.**
  The architecture has no email delivery path; adding it is a product decision
  (see §7). The compensating controls are lockout, token-version revocation,
  and refresh revocation.
* **Rate limiting is keyed by client IP** (`app/core/rate_limit.py`,
  `bucket:{ip}`), with no tenant dimension. This is deliberate for webhook
  endpoints (Twilio/Stripe POST from provider egress IPs, not tenant IPs) and
  documented; a per-tenant authenticated limiter for expensive AI/upload
  operations is listed as hardening in §8.
* **Voice-path tool/turn loop relies on pipecat** plus the new per-call tool
  ceiling; an explicit turn-count limit has not been added to the streaming
  loop.
* **DNS-rebinding and redirect-based SSRF are not covered by the static
  resolver.** The complementary control is egress network policy (the API
  container must not reach link-local/metadata/private ranges); that is an
  operational requirement (see §8), not something a string check can promise.
  None of the outbound HTTP clients here auto-follow redirects with
  credentials replayed.

---

## 4. Tenant-isolation findings (item C/D)

* The tenant is **never** a request parameter — it comes from the verified
  JWT (`app/auth/dependencies.py`), or from a tenant-bound routing token for
  webhooks.
* Every object-level read in the API goes through `get_owned(...)`, which
  scopes by tenant and returns **404** for cross-tenant access (avoids an
  existence oracle).
* Knowledge retrieval, analytics, leads, appointments, team, integrations and
  billing all apply the tenant predicate in SQL (`WHERE tenant_id`), not in
  Python after the fact.
* UUID randomness is never treated as authorisation; IDs are opaque handles,
  authorisation is always a tenant-scoped query.
* Cross-tenant adversarial tests live in `tests/test_tenant_isolation.py` and
  assert safe denial. **Status: TESTED.**

---

## 5. Secrets & encryption findings (item J/K)

* **At rest:** CRM and calendar credentials are AES-256-GCM envelopes bound to
  `(tenant_id, provider)`; key material comes from `CRM_ENCRYPTION_KEYS` with
  a `key_id` per key.
* **Rotation:** add a new key as the first entry (it becomes active); old keys
  stay readable so rows can be re-encrypted at leisure. `parse_key_ring`,
  `encrypt_credentials`, `decrypt_credentials` implement it; `tests/test_crm_credentials.py`
  proves old ciphertext stays readable across rotation and that a ciphertext
  copied into another tenant fails authentication. **Status: TESTED.**
* **In transit:** TLS terminates at Caddy; `uses_https` drives the Secure
  cookie flag and HSTS.
* **In APIs/logs:** response models are allowlists; structured logging
  redacts known secret keys/prefixes and configured secret values; nothing
  writes secrets to logs, exports, or the dashboard. **Status: TESTED.**
* **Never:** rotate real keys automatically, or write credentials back to any
  API response.

---

## 6. AI / tool security findings (item I/L/M/N)

* The LLM is **never** the source of truth for authorisation — every tool
  action re-derives the tenant from the call context and hits tenant-scoped
  queries.
* Tool dispatch is allowlisted (`DISPATCHABLE_TOOLS`), and the voice path has
  a per-call tool ceiling (`MAX_TOOL_CALLS_PER_CALL`).
* Documents are treated as **untrusted reference data**: excerpts are fenced,
  injection-shaped lines are neutralised, and the system prompt explicitly
  forbids acting on document instructions (price changes, transfers, tool
  calls, prompt disclosure). **Status: TESTED**
  (`tests/test_knowledge_grounding.py`, `tests/test_security_regression.py`).
* `max_tokens` is bounded (110 voice / 400 text) and SMS replies are length-
  clamped; the knowledge context budget is clamped at sentence boundaries.
* Prompt-injection cross-tenant exfiltration is prevented structurally: a
  document from tenant A is never retrieved for tenant B, so it cannot be
  summarised for B.

---

## 7. Accepted risks (item U/V)

These are deliberately pinned and monitored; each has a remediation plan and a
reason it is not fixed. **Step 10 (release hardening) resolved R1–R4** (see
§7.1); the accepted set is now down to three dependency residuals plus the
product/operational items below.

| # | Risk | Why accepted | Remediation |
|---|---|---|---|
| R2b | `pillow 11.3.0` (transitive via `pipecat-ai 0.0.94`) | pipecat pins `Pillow<12,>=11.1.0`; the fixes are all in 12.x. VoxDesk's audio-only pipeline never decodes images (no image upload or vision path reaches Pillow), so the image-format advisories are not reachable. | Bump once a pipecat release permits `Pillow>=12`; re-run the full suite. |
| R5 | `pytest 8.3.4` (test-only) | Not shipped; PYSEC-2026-1845 is tmpdir handling in the test runner. The fix is the 9.0.3 major line, which needs a pytest-asyncio migration. | Bump with the next dev-tooling refresh (pytest 9 + pytest-asyncio 1.x). |
| R10 | `nltk 3.10.3` (transitive via `pipecat-ai 0.0.94`) | OSV marks 3.10.3 as the **fixed** version and it is the newest 3.x available, so the pip-audit finding is a boundary false positive; there is no newer release to move to within pipecat's `nltk<4,>=3.9.1`. | Re-check when nltk 4.x exists and pipecat permits it. |
| R6 | No email path, so no password reset / email verification / MFA | Product scope; lockout + revocation compensate | Future feature, not a code fix |
| R7 | IP-keyed rate limiting (no tenant dimension) | Webhooks arrive from provider egress IPs; a tenant key would break them | Per-tenant authenticated limiter for AI/upload ops (§8) |
| R8 | Static SSRF resolver cannot see DNS rebinding or redirects | Fundamental to a no-resolve check | Egress network policy at the container/network layer (§8) |
| R9 | Voice-path turn-count limit not added | pipecat manages turns; per-call tool ceiling added | Explicit turn cap if pen-testing shows need |

### 7.1 Resolved in Step 10

| Former risk | Resolution | Evidence |
|---|---|---|
| R1 `pipecat-ai 0.0.55` | → **0.0.94** | Full import surface, provider constructors, transports, VAD, serializer, context aggregators, metrics frames and the function-calling seam verified against the 0.0.94 wheel; app imports moved to canonical package paths; full backend suite green (2254 passed). |
| R2 `aiohttp`, `pillow` | aiohttp now clean at **3.14.3**; pillow remains (→ R2b) | `pip-audit` no longer flags aiohttp. |
| R3 `cryptography 44.0.1` | → **50.0.1** | Seven advisories closed (OpenSSL-in-wheel, subgroup, DNS-name-constraint, wildcard-DNS verifier, duplicate intermediates, PKCS#7 Bleichenbacher). Only `AESGCM` is used (`app/integrations/crm/crypto.py`), API unchanged 44→50; envelope format unchanged, no migration. `tests/test_crm_credentials.py` + security regression green (41/41). |
| R4 `starlette 0.41.3` | → **1.6.0** via **fastapi 0.136.1** | Seven starlette advisories closed (multipart DoS, Range DoS, BadHost, StaticFiles UNC-path SSRF, HTTP-method dispatch, form() limits, authority poisoning). FastAPI 0.136.1 is the newest release that still flattens `app.routes` at import (0.137.0+ lazy `_IncludedRouter` would break the Step 9 route-contract tests) while permitting starlette 1.x. Full API/middleware/WebSocket/security-header/TrustedHost/CORS/auth suite green. |

The CI dependency gate (`scripts/audit_dependencies.sh`, wired into
`.github/workflows/security-scan.yml`) hard-fails on anything **not** in the
accepted list above, so the accepted set is explicit and reviewable, never
silent.

---

## 8. Operational requirements still open after Step 10 (not complete, not claimed complete)

These are engineering/operational items that remain open and are **not**
claimed as done:

1. **Egress network policy** — restrict the API container so it cannot reach
   link-local (`169.254.0.0/16`), metadata, or RFC 1918 ranges; this is the
   complementary control to the static SSRF resolver (R8). The allowed-domain
   inventory and policy text now live in `docs/STEP10-EGRESS-POLICY.md`.
2. **Real human telephony E2E** — the pipecat upgrade itself is now done
   (R1 resolved, Step 10), but the live-call validation is a human-executed
   checklist (`docs/STEP10-E2E-RUNBOOK.md`) and has not been run.
3. **Staging dashboard/alert validation**, **provider pricing population**
   (`docs/STEP10-COST-SOURCES.md`), **real infrastructure deployment drill**
   (`docs/STEP10-DEPLOY-DRILL.md`), **off-site backup sync**
   (`docs/STEP10-BACKUP-SYNC.md`), and the **future Vitest major upgrade** —
   all open operational items.
4. **Per-tenant rate limits** for authenticated expensive operations (uploads,
   AI calls, ingestion) if abuse metrics show the need.
5. **A real, scoped penetration test** by a third party — the pentest
   checklist in `docs/PENTEST-CHECKLIST.md` is the handover artefact; running
   it does **not** constitute a penetration test.

---

## 9. Incident response, retention, deletion (item P/Q/R/S)

* **IR:** `docs/INCIDENT-RESPONSE.md`, `docs/INCIDENT-TRIAGE.md` (severity,
  runbooks); BCP in `docs/BCP.md`.
* **Retention/deletion:** `app/core/retention.py` purges within tenant
  boundaries and never deletes audit rows; `app/api/gdpr_routes.py` provides
  OWNER-gated export (access) and erasure (deletion) per tenant; legal/audit
  holds are distinguished from ordinary retention. `docs/DPA.md` covers the
  data-processing posture.
* **Audit trail:** `AuditEvent`/`AuditLog` rows are append-only from the API
  (there is no create/update/delete route for them); they carry
  actor/tenant/timestamp/action/target/result/correlation and are never
  exported or deleted. **Status: TESTED**
  (`tests/test_security_regression.py` asserts the audit routes are GET-only).
* **Privacy minimisation:** analytics exports aggregates only (no raw PII);
  transcripts are retained per policy and not shared unless opted in.

---

## 10. Compliance-readiness status (the honest version)

* **IMPLEMENTED + TESTED:** tenant isolation, RBAC, encryption at rest, SSRF
  guard, webhook verification/replay, upload hardening, RAG isolation, prompt-
  injection neutralisation, rate limiting, security headers, audit logging,
  retention/GDPR routes, dependency pinning + audit.
* **OPERATIONALLY VERIFIED (partially):** real Postgres migration round-trip
  (CI), smoke script, deployment/rollback/backup scripts (written and
  unit-validated; the real infrastructure drill is still open).
* **EXTERNALLY AUDITED:** none.
* **CERTIFIED:** none. **VoxDesk does not hold SOC 2 / HIPAA / PCI / ISO
  certification, and no penetration test has been performed.** See
  `docs/SOC2.md` for the readiness matrix that a future auditor would walk.

---

## 11. Vulnerability handling (item Z)

1. Report → triage via `docs/INCIDENT-TRIAGE.md` severity.
2. Fix on a feature branch off `main`; never weaken a control to make a test
   pass; never ship secrets.
3. Add a deterministic regression test that reproduces the vulnerability.
4. Run the full quality gate (see `docs/SECURITY.md` §12 and the CI).
5. Document the fix and, if a CVE affects a pinned transitive dependency that
   cannot yet be bumped, add it to the accepted-risk table (§7) with a
   remediation date.
6. Never claim a fix for a vulnerability that has not been reproduced and
   re-tested.

## 12. Security validation commands (run from the repo root)

```bash
# SAST (expect: 25 Low — all reviewed false positives; 0 Medium/High)
bandit -r app -l

# Dependency audit (hard-fails on anything outside the accepted-risk list)
bash scripts/audit_dependencies.sh

# Backend quality gate (lint + full suite incl. security regressions)
ruff check app scripts tests
python -m pytest tests/ -q -m "not real_provider"

# Frontend quality gate
cd dashboard && npm run test && npm run build && npm audit --omit=dev --audit-level=high

# Secret scanning (CI: gitleaks)
gitleaks detect --source . --redact
```
