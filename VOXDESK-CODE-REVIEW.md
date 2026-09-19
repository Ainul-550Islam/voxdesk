# VoxDesk — Full Code Review (part by part)

Repo: `github.com/Ainul-550Islam/voxdesk` (4 commits, last 2026-09-15)
Reviewed: **all 789 tracked files** — Python backend, 2 dashboards, 4 native services
(Rust/Go/C++), migrations, infra, tests, docs. Nothing skipped.
Method: static review **plus actually running** every build/test suite that could be run.

---

## TL;DR (সংক্ষেপে)

**কোডটা আশ্চর্যজনকভাবে ভালো** — আর্কিটেকচার, security discipline, আর comment-এ লেখা
reasoning production-grade. কিন্তু **চারটা সমস্যা আছে যেগুলো আপনার নিজের টেস্ট স্যুটই
ধরেছে** (CI না থাকায় কেউ দেখেনি), প্লাস রিপোজিটরিতে অনেক জঙ্ক আর একটা পুরো
"দ্বিতীয় আর্কিটেকচার" যেটা চালুই হয় না।

| Rated area | Score | Notes |
|---|---|---|
| Backend code quality | ★★★★★ | Defensive, idempotent, tenant-safe |
| Security engineering | ★★★★★ | Fail-closed defaults everywhere I checked |
| Tests | ★★★★☆ | 2,421 real passing tests — but 4 red, no CI to catch it |
| Repo hygiene | ★☆☆☆☆ | DB dumps, 15 MB nltk data, Go module cache, binary blobs committed |
| Architectural coherence | ★★☆☆☆ | A whole second platform (routers/services/dashboard) is dead code |
| Docs accuracy | ★★★☆☆ | 49 docs, mostly excellent; README numbers are wrong |

**Bottom line: the code that runs is excellent. The repo it lives in needs a cleanup
pass and CI before it can be trusted to stay this way.**

---

## 1. Verification results (what I actually ran)

| Suite | Result |
|---|---|
| Python backend (`pytest`, pinned `requirements.txt` versions) | **2,421 passed · 4 failed · 43 skipped** (of 2,468) |
| Ruff lint (`app tests scripts`) | **Clean, 0 violations** |
| C++ media-plane (`make test`, `-Werror -Wpedantic`) | **Builds clean · 131,558 checks, 0 failures** |
| React dashboard (`dashboard/`, vitest) | **362/362 passed** |
| React dashboard production build (`vite build`) | ✅ builds (272 kB JS / 80 kB gzip) |
| Next.js dashboard (`dashboard-next/`, vitest + `next build`) | **16/16 passed** · builds ✅ |
| Alembic chain (`0001…0011`) | Linear, consistent `down_revision` chain ✅ |
| Secret scan of all tracked files | ✅ No real keys (only test canaries like `sk_live_LEAKED`) |
| Rust control-plane, Go services | ⚠️ Not buildable in this sandbox (no cargo/go toolchain) — reviewed statically |

### The 4 failing tests — and why each one matters

All four are **real repo defects**, not environment noise. They fail because the
"Step 9" hardening files exist only inside the `STEP9-INFRA-FILES/` and
`STEP9-FULL-DELIVERY/` folders — they were **never applied to the repo root**,
and the 4 `.patch` files at the root were never fully applied either:

| Failing test | Root cause |
|---|---|
| `test_gitignore_covers_secrets_and_dumps` | `.gitignore` lacks `backups/`, `*.dump`, `nltk_data/`, `go/`… → **real Postgres dumps got committed** |
| `test_dockerignore_excludes_secrets_and_dumps` | **No root `.dockerignore`** (exists only in `STEP9-INFRA-FILES/`) |
| `test_env_examples_contain_only_placeholder_credentials` | **`.env.staging.example` missing** at root |
| `test_ci_workflow_gates_and_separates_real_providers` | **No `.github/workflows/ci.yml`** → the repo has *no CI at all* |

⚠️ One extra finding from my first run: with FastAPI ≥ 0.137 the route-introspection
contract tests break — exactly as your own `requirements.txt` comment predicts.
**Your pins are load-bearing; never relax them blindly.** CI would have caught this class of thing.

---

## 2. CRITICAL — fix first

### C1. No CI, and the repo's own safety tests are red
The Step 9 delivery folders contain a complete CI workflow
(`STEP9-INFRA-FILES/.github/workflows/ci.yml`), but it was never moved to
`.github/workflows/`. Consequences:
- The 4 red tests above have been red since ~Sep 15 with no alarm.
- Nothing stops the next commit from leaking another `backups/*.dump`.

**Fix:** copy the STEP9 CI workflow + `.dockerignore` + `.env.staging.example` to the
root, verify `pytest tests/test_deployment.py` goes green, delete the source folders.

### C2. Real database dumps committed to a *public* repo
`backups/voxdesk-20260913-*.dump` (4 files, ~800 KB) are genuine PostgreSQL
custom-format dumps of database `voxdesk_staging` (PG 17.11). Good news: I inspected
the COPY blocks — they are **schema-only, zero data rows** (no user hashes, no PII).
Bad news: the full production schema (all tables, columns, constraints) is public,
and the next dump might contain data. The repo's own test expects them gone.

**Fix:** `git rm backups/*.dump`, add `backups/`, `*.dump`, `uploads/`, `var/`,
`nltk_data/`, `go/`, `chaincheck` to `.gitignore`. History still contains the dumps —
rotate nothing (no secrets found), but be aware.

### C3. Committed junk / generated artifacts (~19 MB of bloat)
| What | Size | Why it shouldn't be tracked |
|---|---|---|
| `nltk_data/` (+ `punkt_tab.zip` 4.2 MB) | 15 MB | Downloadable at setup; belongs in `.gitignore` |
| `go/pkg/mod/` (module **cache**) | 467 KB | Machine-local cache, never source |
| `services/media-plane/libvoxdesk_media.a` | binary | Build output (Makefile regenerates it) |
| `uploads/voxdesk-source-bundle.md` | 2.9 MB | Generated "entire source in one file" dump |
| 4 × `000*.patch` (2,560 lines of git-format patches) | — | Already applied history; delivery artifact |
| `chaincheck` (empty file) + `chaincheck/` (empty dir) | 0 | Accident |

### C4. Quickstart footgun: `.env.example` vs dev compose
`docker-compose.yml` (what `make up` runs) injects `.env` into the api container and
does **not** override `DATABASE_URL`. But `.env.example` points at
`localhost:5432` — inside a container, localhost is not Postgres. A new user
following the README's "৬০ সেকেন্ডে চালু" gets an API that cannot reach the DB.
The Step 9 patch `0001-Step-9-Phase-A…patch` *claims* to fix exactly this line —
it was never applied. (`docker-compose.prod.yml` does this correctly with `db:5432`.)

**Fix:** in dev compose add `environment: DATABASE_URL: postgresql+asyncpg://…@db:5432/voxdesk`
(mirror the prod file), or make `.env.example` ship the compose-host URL with a comment.

---

## 3. HIGH — dead and duplicated architecture

### H1. Three API routers are defined but never mounted
`app/main.py` mounts 15 routers. These three (1,267 lines) are **not in the list**:

- `app/api/agent_management_routes.py` — `/api/agents/*` (agent CRUD, versions, publish/rollback, validate, test)
- `app/api/campaign_routes.py` — `/api/campaigns/*` (full campaign lifecycle, audience, eligibility, plan, results, KPIs)
- `app/api/workflow_routes.py` — `/api/workflows/*`

They only boot inside `tests/test_enterprise_batch01.py`, which mounts them by hand.
In production they are unreachable.

### H2. An entire "enterprise layer" (≈8,000+ LOC) has no runtime path
- `app/domain/` (8 model modules) → used only by the 3 unmounted routers + `app/services/`
- `app/services/` (8 service modules) → used only by the 3 unmounted routers
- `app/orchestration/`, `app/evaluation/`, `app/providers/`, `app/knowledge_ops/` →
  **zero imports from any running code** (only their own tests)

It all passes its isolated tests; none of it executes in the product. Either wire it
in (mount the routers, delete the legacy duplicates) or move it to an
`archive/` folder — right now it's dead weight that reviewers and new hires will
mistake for the real system.

### H3. `dashboard-next/` is completely orphaned
The Next.js dashboard is **not referenced anywhere**: not in the `Dockerfile`
(which builds the old Vite `dashboard/`), not in any compose file, not served by
`main.py` (which mounts `dashboard/dist`). Worse, its API client calls
`/api/campaigns` and `/api/campaigns/{id}/results` — the **unmounted** router from
H1 — so if you ever did deploy it, the Campaigns page would 404.

Meanwhile the old `dashboard/` is healthy, tested (362 tests), built, and calls the
mounted legacy endpoints (`/api/tenants/{id}/campaigns`). **Pick one dashboard and
delete the other**; keeping both guarantees drift.

### H4. Native services are orphans too (but good ones)
- `services/control-plane` (Rust: session, idempotency, rate-limit, retry, registry + a signal hub with tests)
- `services/media-plane` (C++17: jitter buffer, VAD, G.711, FFT, denoiser — **131k checks passing**)
- `services/signal-go`, `services/ops` (Go: signal server, backup verify, status)
- `contracts/proto/voxdesk/...` (5 protobuf files)

None appear in any compose file, Dockerfile, or Python import. They are genuine
engineering, but they are R&D artifacts in a product repo — and `go/pkg` (the module
cache they were built with) got committed on top. Decide: integrate or extract.

### H5. Two parallel campaign APIs
- Legacy: `POST /api/tenants/{id}/campaigns` in `routes.py` — **mounted**, used by the live dashboard.
- New: `POST /api/campaigns` in `campaign_routes.py` — **unmounted**, used by the orphan dashboard.

Same story for campaigns/run vs campaigns/plan. One of them has to win.

---

## 4. REAL BUGS found in live code (functional)

### B1. SMS/WhatsApp history loads the **oldest** 20 turns, not the newest 🐛
`app/channels/messaging.py::load_history`:
```python
select(Turn).where(Turn.call_id == thread.id)
    .order_by(Turn.created_at).limit(MAX_HISTORY_TURNS)   # ← no .desc()
```
Once a thread passes 20 messages, the agent's context **freezes at the start of the
conversation** — it never sees the latest messages. On a medium-length SMS thread
this breaks booking-by-text, the exact feature the module exists for.
**Fix:** `.order_by(Turn.created_at.desc()).limit(MAX_HISTORY_TURNS)` then reverse
the list before returning.

### B2. Texting "yes" triggers an opt-in instead of a conversation reply 🐛
`app/channels/messaging.py`:
```python
START_WORDS = {"start", "unstop", "yes", "subscribe", "optin"}
...
if keyword in START_WORDS and len(keyword) > 2:   # "yes" is 3 chars → passes
```
A customer who replies a plain **"yes"** (the most common conversational reply in
SMS English — "yes, 3pm works") gets *"You're subscribed to X again."* and the
message never reaches the LLM/booking flow. The README brags about not misfiring
on `"please stop by at 3"` for opt-out — this is the same bug class on the opt-in
side. **Fix:** drop `"yes"` from `START_WORDS` (Twilio already handles carrier-level
START/STOP) or require the previous assistant turn to be an opt-in question.

### B3. Raw phone number logged in a DNC path
`app/agent/functions.py::mark_do_not_call` does `log.warning("lead.dnc", phone=phone, …)`
with the **full number**, while `messaging.py`, `notifications.py` etc. all use
`phone.redact()`. The README claims "লগে redacted" — one place violates it, and it's
a legally sensitive endpoint (a DNC request log *with* the number is a liability).
**Fix:** `phone=phoneutil.redact(phone)`.

### B4. Redundant DNC re-filter in `next_callable_leads` (harmless, but telling)
SQL already restricts `status.in_([NEW, QUEUED])`, then Python re-checks
`status is not DNC`. Belt-and-suspenders is fine, but the comment trail suggests
someone patched a real DNC leak defensively instead of enforcing it in one place —
worth a comment cleanup so the invariant's single owner is the query.

---

## 5. Part-by-part notes (what's *good* — because most of it is)

- **`app/core/config.py`** — exemplary. Placeholder detection, fail-closed
  `validate_security()`, production refuses DEBUG logs/localhost URLs/test Stripe keys/
  hashing embedder/missing CRM keys. 36 knobs documented *with reasons*.
- **`app/main.py`** — correct lifespan (validate → migrate policy → seed plans →
  billing config check), docs disabled in prod, SPA fallback refuses to swallow API paths.
- **Auth (`jwt/password/service/rbac`)** — pinned alg, iss/aud/type checks, opaque
  refresh tokens hashed at rest, timing-equalized login, lockout, 12-round bcrypt,
  breach-list tripwire. **IDOR-safe**: every tenant path uses `scoped_permission`,
  which proves `tenant_id == ctx.tenant_id` and audits failures.
- **Telephony** — HMAC on *every* webhook incl. status + outbound-answer (with the
  audit story of why), signed 120-second stream tokens bound to callSid, handshake
  timeout, idempotent call creation, terminal-state machine, two-phase transfer with
  provider proof (`REQUESTED→DIALING→CONNECTED/FAILED`), `<Dial action=…>` callback.
- **Agent pipeline** — lazy pipecat import so API boots without the heavy stack,
  per-call tool ceiling (12), dispatch allowlist (no `getattr` injection), tool-arg
  legacy aliases for zero-downtime deploys, turn persistence isolated in `finally`.
- **Channels** — STOP/START/HELP pre-LLM, durable `MessageWebhookReceipt` replay
  protection, thread race handled via savepoint+unique key. (Minus B1/B2 above.)
- **Knowledge/RAG** — tenant predicate ANDed *inside* the query, UPLOADED→…→READY
  lifecycle, injection-neutralized retrieved text, 1.5 s live-call timeout that
  degrades to "I don't know" instead of fabricating, evals in `tests/evals/rag/`.
- **Billing** — Stripe webhook verify→dedupe(unique receipt)→order-safe apply, usage
  events idempotency-keyed from the call (audit-found double-billing fixed and
  documented), *never* meters inside the live call, inbound never blocked.
- **CRM/Calendar integrations** — AES-256-GCM with `(tenant, provider)` AAD binding
  (ciphertext can't be transplanted between tenants), per-integration HMAC webhook
  routing tokens, retry taxonomy (401/422 never retried), every provider marked
  honestly as "not live-tested" in docs.
- **`requirements.txt`** — best dependency file I've reviewed: every pin justified
  with CVE IDs and version-compatibility reasoning.
- **Compose/Caddy/Docker** — prod/staging files are tight: non-root user, advisory-locked
  migrations, `127.0.0.1`-bound API behind Caddy, metrics token-gated, Grafana off
  the public path, ACME defaults untouched.
- **`scripts/scheduler.py`** — proper separation of worker vs API with rationale.

---

## 6. Documentation drift (low priority, high embarrassment potential)

- README: "**১৪৫ tests passing**" and "`make test` → **৪১১টা টেস্ট**" — actual is
  **2,468 collected / 2,421 passing**. Also "**২০+ API endpoint**" vs ~130 routes,
  "**৫,১০০+ লাইন**" vs ~83k lines in app+scripts+tests. The numbers undersell you —
  update them (after making the suite green).
- `README` references `docs/…` fine, but the `STEP*.md` reports at root (16 files)
  + the two `STEP9-*` folders + patches are delivery artifacts belonging in an
  `archive/` or out of the repo.

---

## 7. Recommended fix order

1. **Today:** apply STEP9 files to root (`.github/workflows/ci.yml`, `.dockerignore`,
   `.env.staging.example`) → confirm `pytest tests/test_deployment.py` green.
2. **Today:** `git rm` the junk (§C3), extend `.gitignore`, fix dev-compose
   `DATABASE_URL` (§C4), delete root `*.patch`, archive `STEP9-*` folders.
3. **This week:** fix B1 (history order), B2 (`"yes"` opt-in), B3 (phone redaction) —
   each is a 3-line fix; add the regression tests your suite clearly supports.
4. **This week:** decide the enterprise layer's fate (mount + dedupe, or archive) —
   and pick one dashboard. Update README numbers.
5. **Later:** wire or extract the Rust/Go/C++ services; consider a schema-parity test
   (`alembic check` against models) since nothing verifies models↔migrations drift
   today; replace deprecated `datetime.utcnow()` calls (9,200+ warnings under 3.13).

---

*Review generated 2026-09-16. Suites were run with the repo's exact pinned
dependency versions; frontend with Node 20.*
