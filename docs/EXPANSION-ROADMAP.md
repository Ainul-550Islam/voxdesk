# VOXDESK Expansion Roadmap — 350K–500K+ LOC

**Status:** PLAN ONLY. This document is an architectural proposal. It changes no
code, no configuration, and no behavior. No line of code referenced here has
been written, and none will be until each phase is approved on its own.

**Audience:** engineering lead / architect, for go/no-go decisions.

---

## 1. Honest baseline (measured, not estimated)

Measured on 2026-09-14 against the working tree (excluding `.venv`,
`node_modules`, `dist`, `.git`, caches, lockfiles, and vendored/generated code):

| Stack | Files | First-party LOC |
|---|---|---|
| Python (FastAPI backend + tests + scripts + alembic) | 303 | ~79,200 |
| Frontend (Vite + React, JSX) | 47 | ~13,200 |
| **First-party source total** | **350** | **~92,400** |
| Docs (Markdown) | 54 | — (not counted as source) |
| Config/ops (YAML/JSON/shell/Docker/Caddy/Make) | ~30 | — (not counted as source) |
| Rust / Go | 0 | 0 |
| Next.js / TypeScript | 0 | 0 |
| C++ / WebRTC | 0 | 0 |

**The gap to the stated target is therefore ~260K–410K lines of genuinely new,
tested, first-party code across three language ecosystems that do not exist in
the repository today.** This is a multi-year, multi-team program, not a "next
step." The plan below breaks it into phases where each phase ships and is safe
on its own, so the existing product keeps working the whole way.

### Current architecture (what the plan must preserve and extend)

- **Backend:** FastAPI app under `app/` — `api/` (routes), `agent/` (Pipecat
  voice pipeline), `auth/`, `billing/` + `providers/` (Stripe), `channels/`,
  `core/` (config/security), `db/`, `domain/`, `integrations/` (CRM/calendar/
  validation), `knowledge/` (RAG + extractors + embeddings + storage),
  `release/` (ops verification), `services/`, `telephony/`.
- **Data:** SQLAlchemy 2 (async) + asyncpg + Alembic migrations (12 revisions).
- **Frontend:** Vite + React dashboard in `dashboard/` (13 pages + components).
- **Ops:** Docker Compose (dev/staging/prod), Caddy, Prometheus/Grafana,
  `scripts/` (deploy, backup/restore/DR, TLS, provider/cost/egress/observability
  verifiers, release gate), CI (backend lint/test, Postgres migration smoke,
  frontend test/build, docker build) + security-scan workflow.
- **Tests:** 2,464 backend tests (2,421 pass / 43 skip), 362 frontend tests.

---

## 2. Target definition and governing principles

**Target:** 350,000–500,000+ first-party source lines across four ecosystems:
Python, Rust/Go, TypeScript/Next.js, C++ (WebRTC media).

**Principles (non-negotiable, inherited from the project's own rules):**

1. **LOC is an output, never a goal.** Every line must implement real,
   exercised functionality with tests, or it is not written. Padding, stubs,
   dead code, or vendored third-party code does not count and will not be used
   to reach the number.
2. **Counting methodology is fixed and reproducible** (so progress is honest):
   first-party source only, measured with `find . -type f -name '*.{py,rs,go,ts,tsx,jsx,cpp,h,hpp}'`
   excluding `.venv/`, `node_modules/`, `dist/`, `.git/`, `__pycache__/`,
   generated protobuf/bindings, lockfiles, and vendored dependencies. Reports
   will quote the command, so the number is auditable.
3. **Nothing existing is removed or weakened.** The Python backend and the
   Vite dashboard keep running and keep passing their suites throughout. New
   systems are added alongside, behind clean contracts (strangler/adapter
   pattern). A migration is only "done" when parity is proven by tests, not
   when new code compiles.
4. **Cross-cutting invariants apply to every new service** (they are not
   Python-specific): tenant isolation, idempotency, the E2E safety guard, the
   release gate (never forced), observability (metrics/logs/traces), backup/
   restore + DR, pricing/usage accuracy, provider compatibility, and "no
   production mutation / no real charges / no real calls" in tests.
5. **No dependency invented when one can be reused.** Native/foreign code is
   added only where the problem demands it (see §4 decision gates).

---

## 3. Target architecture (end state)

```
                        ┌────────────────────────────────────────────┐
                        │        TypeScript / Next.js (web + ops)    │
                        │  customer dashboard · admin console ·      │
                        │  live call wallboard · WebRTC browser client│
                        └───────────────┬────────────────────────────┘
                                        │ HTTPS / WSS / WebRTC (browser)
                        ┌───────────────▼────────────────────────────┐
                        │        Python (FastAPI) — control surface  │
                        │  auth · RBAC · billing · knowledge/RAG ·   │
                        │  campaigns · integrations · GDPR           │
                        └───────┬──────────────────────┬─────────────┘
                                │ gRPC/events (internal)│
              ┌─────────────────▼──────┐      ┌────────▼─────────────────┐
              │  Rust control plane     │      │  C++ media plane (WebRTC)│
              │  session state machine  │◄────►│  SFU · DTLS-SRTP · Opus  │
              │  SIP/SDP signaling      │ RTP  │  jitter buffer · recorder│
              │  usage/rating pipeline  │      │  TURN/STUN · pacing      │
              │  scheduler (high-thru)  │      └──────────────────────────┘
              │  webhook fan-out        │
              └────────────────────────┘
                        │
              ┌─────────▼──────────┐
              │  Go ops tooling     │   CLI/daemons: backup, DR drills,
              │  (or Rust, per §4)  │   migration runner, canary, meta-ops
              └────────────────────┘
```

Data/state: PostgreSQL (existing), Redis (existing), object storage for media
recordings + knowledge, Kafka/NATS-style bus (new) between the control plane
and the media plane. Voice AI (Pipecat) remains Python; it connects to the C++
media plane over RTP, not over HTTP.

---

## 4. Language decision gates (decide before phase start, not during)

These are the points where the "Rust/Go" and "build vs adopt" choices must be
made explicitly. Defaults are recommended; each is reversible before the phase
starts, not after.

| Decision | Options | Recommendation | Why |
|---|---|---|---|
| Control-plane language | Rust vs Go | **Rust** | Latency-critical, memory-safe session/SIP state; strong ecosystem (tokio, tonic, rtp/webrtc-rs crates) for the media-adjacent work. |
| Ops/CLI tooling | Go vs Rust | **Go** (Rust acceptable) | Fast builds, single static binaries, trivial cross-compile for deploy tooling; but keep it **one** language to bound scope. |
| Web frontend | Migrate Vite→Next.js vs build Next.js alongside | **Build alongside, migrate page-by-page** (strangler) | The Vite app is working and tested; a big-bang rewrite violates "don't weaken what works." Next.js app is served for new/admin surfaces first. |
| WebRTC media server | Build SFU from scratch vs extend an established C++ base (libwebrtc / mediasoup-class) | **Extend an established base** for the core transport; write custom C++ only for VoxDesk-specific value (policy, recording, transcription tap, tenant-aware routing) | A from-scratch production SFU is a multi-year security surface (DTLS-SRTP, congestion control). Adopting the transport and writing the differentiators keeps the LOC honest and the risk bounded. |
| Message bus | Kafka vs NATS vs Redis streams (existing) | Decide at Phase 2 start; **NATS** (or Redis Streams to reuse existing infra) | Reuse beats new infra; revisit only if throughput demands it. |

---

## 5. Phase plan

Each phase: **goal → scope → rough LOC → dependencies → exit criteria.**
LOC figures are engineering estimates for first-party source, not commitments
used to force output.

### Phase 0 — Cross-language foundations & contracts (Python-led, additive)
**Goal:** make the monorepo multi-language without disturbing the working
system.

- Repository layout for `services/` (Rust, Go, C++ workspaces) alongside `app/`.
- Wire contracts: protobuf/OpenAPI definitions for every cross-service boundary
  (session, media, usage, webhook). Generated bindings are excluded from the
  first-party LOC count.
- CI matrix extended to build/test/lint all four stacks; shared lint+format and
  license/secret scanning across languages.
- Contract tests and a local `docker compose` multi-service dev topology.
- **Estimated LOC: 12K–20K** (schemas, harnesses, generators, tests).
- **Exit:** `make verify` runs the full 4-language gate on a clean checkout;
  existing Python + frontend suites still green.

### Phase 1 — Next.js/TypeScript surface (strangler migration)
**Goal:** modern frontend without removing the Vite app.

- New Next.js app (TS, strict): auth session, tenant admin console, ops console,
  live call wallboard (WebSocket), billing/usage UI, knowledge/RAG admin,
  campaign builder, and a design system + component library.
- Playwright e2e + component tests; i18n; accessibility; feature parity per page
  before the Vite page is retired.
- **Estimated LOC: 60K–90K** (TypeScript/TSX).
- **Exit:** every Vite page has a Next.js equivalent with parity tests, then
  (and only then) the Vite app is switched off behind a flag; Vite code is
  archived, not deleted, until 2 releases of stability.

### Phase 2 — Rust control plane (session/state/scheduling at scale)
**Goal:** move latency- and throughput-critical work off the Python request
path, behind contracts defined in Phase 0.

- Session state machine (call lifecycle: ringing → connected → transferred →
  ended), SIP/SDP signaling, call transfer orchestration (mirrors existing
  `call_transfer_lifecycle` semantics exactly), high-throughput scheduler
  (replaces the asyncio loops in `scripts/scheduler.py` only when it can prove
  parity), usage/metering rating pipeline, webhook fan-out with retries and
  idempotency, rate limiting.
- Property-based tests for the state machine; differential tests against the
  existing Python behavior (same inputs → same observable outcomes).
- **Estimated LOC: 80K–120K** (Rust).
- **Exit:** shadow-mode runs show identical behavior to Python on production
  traffic traces; kill switch restores Python path in <5 min.

### Phase 3 — C++ WebRTC media plane
**Goal:** own the audio path end-to-end (browser ↔ Pipecat agent).

- Media server: DTLS-SRTP, Opus jitter buffer/pacing, mixing, server-side
  recording sink (object storage), TURN/STUN, tenant-aware routing, and a
  transcription tap that feeds the existing pipeline.
- Thin language bindings (Rust FFI or a small Python/Node shim) that the
  control plane and Pipecat use; the browser client is the TypeScript side from
  Phase 1.
- **Estimated LOC: 80K–120K** (C++ first-party; excludes the adopted base).
- **Exit:** calls route through the media plane with bounded latency and
  packet-loss targets in staging; the existing Twilio/telephony path is
  retained and selectable per-tenant during the transition.

### Phase 4 — Python core deepening (AI + compliance + analytics)
**Goal:** deepen the differentiated product surface that justifies the rest.

- Agent workflow/campaign orchestration engine, prompt & evaluation pipelines,
  knowledge-graph + vector-store operations, multi-provider abstraction
  hardening, GDPR automation (expanded), analytics/ML (conversation metrics,
  churn/usage forecasting).
- **Estimated LOC: 40K–60K** (Python).
- **Exit:** full backend suite (currently 2,464 tests) grows in proportion;
  every new module is tested, observable, and behind the release gate.

### Phase 5 — Go (or Rust) ops & resilience tooling
**Goal:** production-grade ops independent of the Python runtime.

- Backup/restore and DR drill tooling (parity with `scripts/backup*.sh` +
  `scripts/restore.sh`, then surpass), migration runner, canary/deploy
  controller, meta-ops (cert rotation, secret scanning, cost report generator
  matching `scripts/verify_cost_config.py` semantics).
- **Estimated LOC: 20K–40K** (Go/Rust).
- **Exit:** DR drill automation completes in staging end-to-end; the release
  gate consumes the same evidence the Python verifiers produce today.

---

## 6. Cumulative tracking (honest, auditable)

| Milestone | Cumulative first-party LOC (approx.) |
|---|---|
| Today (baseline) | ~92,400 |
| After Phase 0 | ~105K |
| After Phase 1 | ~175K |
| After Phase 2 | ~275K |
| After Phase 3 | ~375K |
| After Phase 4 | ~425K |
| After Phase 5 | ~455K (target band 350K–500K+) |

Each milestone is reported with the exact `find … | wc -l` command from §2 so
the number cannot be inflated by generated or vendored code.

---

## 7. Cross-cutting invariants carried into every phase

- **Tenant isolation** — every new service enforces tenant scoping at the
  contract level; no cross-tenant data path.
- **Idempotency & exactly-once side effects** — mirrors existing
  `0011_side_effect_exactly_once` guarantees in billing/webhooks.
- **E2E safety guard** — no real phone calls, charges, or production mutation
  from any test or tool; load/media tests are loopback-gated as today.
- **Observability** — Prometheus metrics, structured logs, traces on every new
  service from day one (reusing existing Prometheus/Grafana stack).
- **Backup/restore & DR** — new state (media recordings, control-plane state)
  is captured by the DR contract.
- **Release gate** — never forced; new-language gates are added to it, not
  bypassed.
- **Security** — no hardcoded credentials; SSRF/egress guards extended to new
  services; dependency auditing per language (already wired for Python/npm,
  added for cargo/go modules/crates).

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Rewriting working Python in Rust/Go "because" | Phase 2/5 migrate only when differential/shadow tests prove parity; Python path stays as kill switch. |
| Big-bang frontend rewrite breaks the working dashboard | Strangler migration, page-by-page parity tests (Phase 1 exit criteria). |
| From-scratch SFU security/performance failure | Adopt an established C++ base; write only differentiators (Phase 3). |
| Line-count pressure producing padding | Fixed counting methodology + "LOC is output only" principle; padding is explicitly out of scope. |
| Multi-language CI/operational burden | Phase 0 builds the 4-language gate first; one language per problem (decision gates in §4). |
| Scope sprawl across 5 phases | Each phase has a go/no-go decision gate with its own exit criteria; this document does not pre-approve any code. |

---

## 9. What this plan deliberately does NOT include

- No placeholder/stub/`// TODO` code to inflate line counts.
- No deletion of the existing Python backend, Vite dashboard, tests, scripts,
  or the release/verification tooling.
- No "rewrite it in Rust for its own sake" — migrations are parity-driven only.
- No invented third-party dependencies; reuse is the default (§4).
- No production changes, real calls, or real charges as part of building any
  phase.

---

## 10. Suggested sequencing & next concrete step

Recommended order is **0 → 1 → 2 → 3 → 4 → 5** (contracts first, frontend
early for user-visible value, media plane only after the control plane and
contracts exist). Phases are independent enough that 1, 4, and 5 could run in
parallel with 0 once contracts are stable.

**The single next step, if approved:** write the Phase 0 design spec — repository
layout for `services/`, the contract (protobuf/OpenAPI) inventory, and the
4-language CI matrix — as a second planning document, still without touching
existing code.
