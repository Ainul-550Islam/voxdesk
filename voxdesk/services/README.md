# services/ — polyglot workspace

This directory is the home of the non-Python services introduced by the
expansion roadmap. The wire contracts live in `contracts/proto`; each language
directory below is an independent workspace with its own build, lint and test,
invoked by the CI polyglot workflow (`.github/workflows/polyglot.yml`).

## Layout

```
services/
├── control-plane/   Rust — session state machine, SIP/SDP signaling,
│                    usage/rating pipeline, webhook fan-out   (roadmap Phase 2)
├── signal-go/       Go — real-time WebSocket signaling hub, differential
│                    counterpart of control-plane's voxdesk-signal
├── realtime/
│   └── gateway-go/  Go — PUBLIC WebSocket edge for dashboards: JWT-gated
│                    hello, tenant-pinned rooms, ingest fan-out from the API
├── media-plane/     C++ — WebRTC SFU, DTLS-SRTP, Opus jitter buffer,
│                    recording sink, transcription tap        (roadmap Phase 3)
├── ops/             Go — ops & resilience CLI (voxops), Step 13
│                    evidence parity                          (roadmap Phase 5)
└── web/             TypeScript/Next.js — admin console, wallboard,
                     WebRTC browser client                    (roadmap Phase 1,
                                                              lives in dashboard-next/)
```

Each service generates its bindings from `contracts/proto` rather than
hand-rolling message shapes, and each keeps the same invariants the Python
backend already enforces, expressed in its own language's tooling.

## The polyglot gate

1. **Tenant isolation** — every message is tenant-scoped (contract-level rule,
   enforced by `scripts/verify_contracts.py`).
2. **Idempotency / exactly-once side effects** — mirrors the database
   `UniqueConstraint(tenant_id, idempotency_key)` guarantees.
3. **Observability** — Prometheus metrics, structured logs and trace ids on
   every service from day one.
4. **No production mutation from tests** — same E2E safety posture; no real
   calls, charges or bookings from any test or tool.
5. **Reproducible builds** — pinned toolchain and dependencies per language,
   wired into CI.

## Current state and next steps

- [x] Wire contracts (`contracts/proto`) — compiled + cross-checked.
- [x] Contract verifier (`scripts/verify_contracts.py`) + contract tests
      (`tests/test_contracts.py`).
- [x] CI polyglot workflow (`contracts`, `media-plane`, `control-plane`,
      `signal-go`, `ops-go`, `dashboard-next` jobs).
- [x] `services/media-plane/` — C++ media plane (Phase 3): jitter buffer +
      tenant consistent-hash router, plus the audio/video processing modules
      (radix-2 FFT, spectral-subtraction denoiser, G.711 mu/A-law codec,
      min-statistics energy VAD); `g++` + `make`, both test binaries green
      (1175 + 130k checks).
- [x] `dashboard-next/` — Next.js/TypeScript dashboard (Phase 1): all 13
      Vite pages ported (Overview, Calls, CallDetail, Analytics, Appointments,
      Campaigns, Leads, Knowledge, Integrations, Agent, Team, Billing, Audit)
      + typed API client + strict TS; `next build` + vitest green.
- [x] `services/control-plane/` — Rust control plane (Phase 2): `voxdesk-control`
      std-only core (session/transfer state machine mirroring
      `app/telephony/call_state.py` + `transfer_service.py`, tenant-scoped
      registry, idempotency guard, bounded broadcast, interval scheduler
      mirroring `scripts/scheduler.py`, usage/metering + rating mirroring
      `app/billing/metering.py`+`plans.py`, token-bucket rate limiter,
      backoff retry) + `voxdesk-signal` (tokio + tungstenite WebSocket hub with
      tenant-scoped rooms, plus `session.proto`-shaped JSON messages in
      `protocol.rs`); `cargo fmt` + `clippy -D warnings` + 56 tests green
      (53 core incl. differential/property session-parity tests + 3 signal).
- [x] `services/signal-go/` — Go real-time signaling hub: same tagged-JSON
      wire protocol and tenant-scoping rules as `voxdesk-signal` (hello once,
      `hello_required`, backpressure-drop, 60 s idle reap), plus token-bucket
      rate limiter and exactly-once idempotency guard; `gofmt` + `go vet` +
      `go test -race ./...` + `go build` green (37 tests, incl. concurrent
      publish tenant-isolation smoke test).
- [x] `services/realtime/gateway-go/` — Go PUBLIC WebSocket edge, two planes
      over one socket: (1) NOTICE — HS256 JWT hello (same tokens the API
      mints, alg pinned, iss/aud/exp/typ enforced), tenant pinned from the
      token, closed room namespace, per-tenant + global connection caps,
      frame limiter, heartbeat with pong-timeout reaping and token-expiry
      close, Bearer-gated `/metrics`, secret-gated `/ingest/v1/publish` with
      `(tenant, event_id)` replay suppression; (2) SIGNALING — point-to-point
      WebRTC session setup between two same-tenant connections
      (`session.start/join/end`, offer/answer/candidate relay byte-verbatim,
      UUID-capability session ids, one-outstanding-offer glare guard,
      socket-owned liveness with `peer_disconnected`/`join_timeout` reaping,
      tenant-mismatch/non-member/unknown-id collapsed to session_unknown);
      fail-closed boot on missing/placeholder secrets; `gofmt` + `go vet` +
      `go test -race ./...` + `go build` green (107 tests, incl. full-socket
      round trips for both planes and wire-level tenant isolation).
- [ ] `services/ops/` — Go module + first CLI (backup/restore verification).

Language toolchains are intentionally installed per-phase; see
`docs/EXPANSION-ROADMAP.md` for the sequencing and decision gates.
