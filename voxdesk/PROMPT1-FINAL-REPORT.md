# Prompt 1 — Final Report: Realtime Foundation Audit & Fix
**Date:** 2026-09-17 · **Scope:** services/realtime/gateway-go + services/realtime/media-engine-rs + deploy wiring · **Rule set:** Prompt 1 A–J, 17-point done-checklist

---

## 1. Gates (exact commands + results)

| Gate | Command | Result |
|---|---|---|
| Rust tests | `cd services/realtime/media-engine-rs && cargo test --workspace` | **89 passed, 0 failed** (was 88 at baseline; old engine pipeline tests replaced & expanded) |
| Rust fmt | `cargo fmt --all -- --check` | clean |
| Rust lint | `cargo clippy --workspace --all-targets` | **0 warnings** (12 pre-existing style warnings fixed during the phase; aes128 crypto constant math untouched — warnings there were noise-auditable, but resolved globally via std idioms where semantically identical) |
| Go tests | `cd services/realtime/gateway-go && go test ./...` | all 19 packages ok |
| Go race | `go test -race -count=1 ./...` | **all 19 packages ok, 0 failures** |
| Go fmt/vet | `gofmt -l .` / `go vet ./...` | clean / clean |
| Verbose count | `go test -v ./...` | **177 `--- PASS`, 0 FAIL** (includes subtests; prior baseline was 149 under the same counting) |
| Live Go↔Rust | `VOXDESK_IT_ENGINE=1 VOXDESK_IT_ENGINE_BIN=... go test -tags it -run TestLive ./internal/engineclient` | **PASS** — real engine binary: health probe → join (ready + ICE creds) → offer→answer → valid trickle quiet success → malformed trickle `bad_message` → leave |
| Engine smoke | curl against built bin | `/v1/health` payload ✓, enveloped join echoes correlation id ✓, `v:9` → `unsupported_version` structured error ✓, new metric series exposed ✓ |
| Compose validation | `docker compose config` | **COULD NOT RUN — no docker client in this sandbox** (verified: `docker: command not found`). YAML parsed successfully with PyYAML; service graphs hand-checked against existing compose conventions. Docker build of the new media-engine Dockerfile therefore NOT executed here — flagged honestly per prompt section I. |
| Tooling note | mid-phase, the sandbox snapshot wiped Rust toolchains and Go (1.27.1). Both reinstalled (rustup stable 1.98.1; go 1.27.1 → `~/toolchains/go`). No gate results were ever claimed before the reinstall. |

## 2. Files modified (COMPLETE contents live in the repo — nothing omitted)

**Rust — media-engine-rs**
| File | Lines | What changed |
|---|---|---|
| crates/protocol/src/lib.rs | 351 | `WIRE_VERSION=1`; Publish/Subscribe/Unsubscribe/Leave gained additive optional `session` field (v1.1 wire, decode back-compatible) |
| crates/webrtc/src/ice.rs | 414 | `add_remote_candidate` (validate/dedupe/persist), `remote_candidates`, per-agent unique txn ids (`next_transaction_id` replacing compile-constant `b"voxdesk-ion!"`) |
| crates/signaling/src/lib.rs | 547 | real Trickle parse/validate/refusal path; publish/leave/offer produce Effects (`published`, `left`, `trickle`, `offer_context`); session-attribution enforcement (`require_session`) |
| crates/engine/src/lib.rs | 861 | **rewritten**: slot SSRC mint on publish, `by_ssrc` map (kills `iter().next()` arbitrary track routing), endpoint anti-spoof gate, RTCP parse→discriminate→apply (SR/RR counters, report blocks applied to source streams, unsupported counted), leave/sweep full teardown, `on_control` versioned envelope (`{v,id,frame}`/`{v,id,frames}` + structured errors; bare-frame back-compat), `health_json` |
| crates/engine/tests/pipeline.rs | 404 | **rewritten**: 4 suite tests — full join→trickle→offer→publish→subscribe→RTP fanout w/ `ssrc` identity assertions, spoof/unknown counters, RTCP RR record assertions, multi-track allocator & per-track routing, envelope contract, health payload |
| crates/media/src/lib.rs | 194 | SourceStream gained RR/SR feedback record fields |
| crates/signaling/tests/flows.rs | ~150 | publish/subscribe constructors carry `session: None` (ctx-attached path); trickle test updated to new refusal/accept semantics |
| crates/protocol/tests/wire.rs | ~70 | constructor field updates only |
| crates/routing/src/lib.rs, webrtc/src/srtp.rs, rate-limit/src/lib.rs | — | clippy idiom fixes only, no semantic change |
| bins/media-engine/src/main.rs | 298 | `/v1/health` route; `POST /v1/signal` now delegates to `on_control` (envelope pass-through); metrics exposition gained rtcp/drop kind series |
| bins/loadgen/src/main.rs | 197 | bench registers tracks through the REAL publish frame (session-attributed); reads the engine-minted SSRC rather than poking slot internals |
| Dockerfile, scripts/build-media-engine.sh | new | two-stage non-root image; IT build helper |

**Go — gateway-go**
| File | Lines | What changed |
|---|---|---|
| internal/engineclient/client.go | 432 | **NEW**: typed HTTP client for the engine wire (correlation ids, typed `EngineError`/`FrameError`, deadline discipline = caller ctx ∪ configured backstop, no blind retry, availability state machine `Monitor` w/ backoff 200ms→5s + Up()/LastError()/LastHealth(), `IsUnavailable` classification) |
| internal/engineclient/client_test.go | 217 | **NEW**: 9 tests (envelope shape, id echo, mismatch refusal, in-band error typing, version skew, ctx-deadline-governs, monitor transitions, availability states, exact one-hit [no hidden retry]) |
| internal/engineclient/it_live_test.go | 128 | **NEW** (build tag `it`): live test vs real engine binary, env-gated |
| internal/config/config.go | 317 | `MediaEngineURL`, `MediaEngineTimeoutSeconds` (+ `envFloat` helper); scheme-required problems, timeout bounds (0,30], disabled-plane warning when absent |
| internal/config/config_engine_test.go | 87 | **NEW**: 4 validation tests |
| internal/server/server.go | 191 | `SetEngine`/`Engine` (public `New` arity unchanged — contract preserved) |
| internal/server/health.go | 75 | readiness engine segment: disabled → ready; up → ready + version/rooms; **down → 503 with last_error** |
| internal/signaling/router.go | 157 | engine attach (`SetEngine`), `engineSessions` map, engine cleanup guarantee on ConnClosed (pending-session socket close) |
| internal/signaling/events.go | 224 | `engineJoinHook` (inline, 1.2s bound, on EvStarted/EvJoined), `engineLeaveHook` (pre-lookup-guard on every EvEnded), `engineLeaveAllForConn` safety net, UNAVAILABLE-vs-REFUSED log classification, no ICE creds in logs |
| internal/observability/metrics/metrics.go | 205 | engine series: up gauge, calls/errors (join/leave breakdown), probes, latency sum/max |
| internal/server/engine_e2e_test.go | 377 | **NEW**: 6 integration tests over REAL websockets + scripted engine |
| cmd/gateway/main.go | 157 | engine client construction + Monitor goroutine + observer adapter, deterministic boot logging |

**Deploy**
- `docker-compose.prod.yml`: new `media-engine` service (UDP 5000 published, control 9001 inter-container, `MEDIA_ENGINE_PUBLIC_IP:?required`); gateway gains `VOXDESK_GATEWAY_MEDIA_ENGINE_URL=http://media-engine:9001` + timeout + depends_on.
- `.env.example`: appended MEDIA_ENGINE_* block (documented, no insecure defaults — public IP is required-empty).

## 3. Bugs found → fixed (Rust engine)

1. **`tracks.iter().next()` arbitrary routing** → deterministic SSRC→(session,track) map; allocation counter; collision walk.
2. **Publish accepted but never registered into the forwarding path** → `on_published` mints ssrc, updates slot ledger + by_ssrc atomically per publish.
3. **RTCP silently discarded** (classify→drop) → full parse; SR/RR counters; RR report blocks applied to source stream records (`rr_fraction_lost_latest`, `rr_cumulative_lost_latest`, `rr_total`); unsupported packet types and malformed counted explicitly.
4. **Fixed STUN transaction id `b"voxdesk-ion!"`** (RFC 5389 violation; two outstanding checks could collide) → per-agent atomic counter + tiebreaker mixing, never repeating within a ufrag.
5. **Trickle frames swallowed uncommented** → parse, session-state validation, candidate validation, dedupe into ICE pair table, persisted to agent; malformed → `bad_message`; e.o.c. markers; wrong-session → `wrong_state`.
6. **Offer reduce threw away remote context** → ICE creds/candidates harvested into agent (`adopt_remote` on the session's agent; trickle-after-offer via `add_remote_candidate`).
7. **Leave never tore down wire identity** (endpoint binding and ssrc ownership survived) → leave sweeps endpoint, ssrc owners, and stream pipeline state; session sweep does the same.
8. **No anti-spoof on RTP**: any 5-tuple could claim an ssrc → datagrams are refused unless source equals that session's ICE-nominated endpoint; separate `rtp_drop_spoof` counter.
9. **No correlation/versioning on the control wire** → `{v,id,frame}`/`{v,id,frames}` + structured `{v,id,error}`; mismatched/unknown version refused in-band; bare frames still accepted for the legacy smoke path.
10. **No readiness payload for the Go side** → `GET /v1/health` reports wire v, version, ready, rooms/participants/tracks/streams, uptime.

## 4. What "real Go↔Rust integration" consists of
- Transport: engine's documented HTTP control (`POST /v1/signal` envelope + `GET /v1/health`), as chosen in the plan (least invasive).
- Explicit protocol: versioned envelope both directions, 16-hex correlation ids echoed and verified — a mis-correlated reply is a `FrameError`, never delivered.
- Timeout/cancellation: caller ctx rules; client backstop = `MEDIA_ENGINE_TIMEOUT_SECONDS` (default 1.5s); router-side per-call cap 1.2s so engine slowness can never stall a connection's read loop beyond that.
- Retry/idempotency: no blind retries inside the client (joins mint state); engine leaves are idempotent by design (unknown session = quiet success), so retry-at-ambiguity is caller-safe. Documented in code.
- Reconnect/liveness: `Monitor` probes `/v1/health` with 200ms→5s backoff; one transition log + gauge flip per change; fail-closed on boot (never claims up before first green probe).
- Lifecycle: EvStarted/EvJoined → engine join (room=`tenant:session`, participant=connID); EvEnded/ConnClosed → leave (with pending-session safety net). Inline, bounded ≤1.2s per hook.
- Graceful degradation: down/refusing engine never breaks session establishment; failures surface via metrics + classified structured logs + readiness going red (503) — exactly the three channels.
- Metrics: `voxdesk_gateway_engine_up`, `engine_signal_calls_total`, `engine_signal_errors_total`, join/leave totals+errors, probe totals+errors, latency sum/max. Engine-side: stun/rtp/unknown + new `voxdesk_media_rtcp_total{kind}` and `voxdesk_media_rtp_dropped_total{kind}`.

## 5. 17-point done checklist
✔ tracks actually registered · ✔ real SSRC routing (no `iter().next()`) · ✔ RTCP parsed/applied, nothing silently discarded · ✔ trickle persisted to agent pair table · ✔ unique ICE txn ids · ✔ REAL Go↔Rust path (live test green) · ✔ config points at real engine in prod compose · ✔ readiness reflects engine (down → 503) · ✔ no insecure defaults (empty-URL = explicit disabled + warning; JWT/ingest refusals unchanged) · ✔ no placeholders/TODO in touched paths · ✔ no fake implementations · ✔ public contracts preserved (`server.New` arity; bare-frame control back-compat; client-visible frames unchanged/additive) · ✔ race gate green (go vet + `-race`, Rust shared-state is single-owner + Arc/Mutex) · ✔ secrets out of logs/metrics (ICE pwd never logged; classified error classes only) · ✔ full-file contents = repo state · ✔ integration results reported including the one gate that could not run here (docker client absent — compose not executed). ✘/N/A: none unaccounted.

## 6. Deltas the client surface will notice
**None by contract.** Browser frames unchanged; session lifecycle frames unchanged; engine integration is fully server-side additive. The only wire evolution: engine control protocol now stamps `{v,id,…}` (gateway↔engine internal only).

## 7. Prompt-2 remainder (explicit)
1. Browser offers currently continue gateway P2P relay; the SFU steer (offers answered by the engine, answers pushed as first-class gateway frames) needs a v1.2 of the browser protocol + engine m-line batching. **Not done this phase by design** (prompt E scope chosen: join/leave/health + real wiring).
2. DTLS-SRTP key extraction feeding `bind_srtp` (engine hook exists; SDES-bound in tests).
3. Engine→gateway `track.published` fanout delivery channel (engine emits; gateway does not yet subscribe for browser-visibility).
4. Multi-node engine fleet: SSRC allocator is per-node; cross-node namespace is label-seeded but untested at fleet scale.
5. Loadgen numbers re-verified only locally (MemTransport); NIC-scale rerun in staging advised, with the new metric series wired into the Grafana dashboard.
6. Compose/docker build could not be executed in this sandbox — first deploy should run `docker compose -f docker-compose.prod.yml config` and the image build in CI.

---

# Addendum 2026-09-17 (Prompt-2 executable leg CONTINUED)

## Executed this leg
1. **Fleet-scale SSRC partitioning (item 4) — DONE.** The allocator is now `(partition_byte << 24) | counter24` where `partition_byte = FNV-1a(engine_label) mod 254 + 1` (skips 0x00/0xFF): 255 fleet slots, 16.7M SSRCs/node, restart collision still gated by the by_ssrc walk. Partition exposed via `Engine::ssrc_partition()` and `health_json`'s new `ssrc_partition` field so two colliding nodes are diagnosable from metrics. New test `fleet_ssrc_partitions_do_not_overlap_across_labels` (two labels with distinct partitions, 1024 sampled allocations, disjoint sets, top-byte assertions).
2. **Observability wiring (item 5, sandbox-executable half) — DONE.** `observability/prometheus.yml` gained the `voxdesk-media-engine` job; engine gauge series renamed to namespaced `voxdesk_engine_{rooms,participants,tracks}_current` (comment and names now agree; live curl-verified). New gateway-side engine series from Prompt-1 unchanged.
3. **Loadgen rerun (item 5, local half) — DONE.** Post-change: **328,292 pps** (baseline 315k — noise band), accounting exact: injected 280,000 (+4,375 replays), received 284,375 = n + n/64 ✓, duplicates 4,375 ✓, unknown_frames 0 ✓. NIC-scale staging rerun still pending (infra).
4. **Prompt-2 heavy items (1: SFU steer; 2: DTLS-SRTP extraction) — specified, not built.** `PROMPT2-DESIGN.md` contains: browser-protocol v1.2 frame vocabulary, gateway/engine deltas by file, the rejected-vs-chosen fork on the engine push problem (decision: A1 single `/v1/bus` chunked stream per engine node, idempotent `?since` resume, token header required in prod), rollout flags (`VOXDESK_GATEWAY_ENGINE_STEER=off|v1.2|force`), test plans, and the explicit constraint lift for the DTLS crate (pure-Rust WebRTC dtls, fingerprint-vs-SDP binding, RFC 5764 extraction into the existing `bind_srtp` hook). Nothing is faked.

## Gates at leg end (exact)
- Rust: `cargo test --workspace` → **90 passed, 0 failed** (+fleet test); `cargo fmt --all -- --check` clean; `cargo clippy --workspace --all-targets` → **0 warnings**.
- Engine smoke: metrics series verified by curl (14 named series listed above).
- Go: no code touched this leg; prior `-race` gate from the completion pass stands.
- Sandbox reality: toolchains (Rust + Go 1.27.1) are pruned between turns by the snapshot; `restore-tooling.sh` at repo-root-adjacent `~/restore-tooling.sh` now self-heals them in ~12s (documented for any continuation).

## Not done this leg (carried verbatim into Prompt-2 execution)
- SFU steer and DTLS-SRTP remain **designed, not implemented** (PROMPT2-DESIGN.md is the implementation contract).
- Staging loadgen (NIC-scale), CI `docker compose config` + engine image build — infra access required.
- live `it` lane (browser-free go↔rust) from the completion pass still passes and remains the engine/client CI gate.

---

# Addendum 2026-09-17 (leg 3: PROMPT-2 ITEM 1 — SFU STEER — IMPLEMENTED)

Prompt-2 item 1 (browser media through the Rust SFU) is now CODE-COMPLETE per PROMPT2-DESIGN.md's amendment: full wire 1.2 (hello ws:2 marker, 5 engine.* client frames, 2 new server frames, steer flag on join acks), negotiation/mixing refusal/degradation rules exactly as designed, config+compose+env wiring, deliberate golden updates. Design amendment recorded: the `/v1/bus` fork (A1) was not built — the engine's reply+room_fanout response array already carries every event the 2-member model needs; the bus stays designed for the multi-member feature line.

Gates: `go test -race -count=1 ./...` → 19/19 packages ok, **186 PASS / 0 FAIL**; live `-tags it` lane against the real engine binary → PASS (incl. publish fanout against real Rust behavior); rust gates unchanged (90/90); vet/gofmt clean; one dev-time test race caught by -race and fixed.

Remaining Prompt-2 scope: item 2 (DTLS-SRTP extraction — required before steer=force with real browsers, since browsers refuse non-DTLS media), dashboard wire-1.2 adoption (TS client work — out of this workspace's scope rules), fleet + staging verification (infra), browser UA integration tests in the it lane.

---

# Addendum — leg 4 (2026-09-17): Prompt-2 item 2, DTLS-SRTP termination — SHIPPED

Prompt-2 item 2 (RFC 5764 DTLS-SRTP key extraction in the media engine) is now CODE-COMPLETE per PROMPT2-DESIGN.md's second amendment: one pinned dependency tree (`dimpl` 0.7.3, Sans-IO sync DTLS 1.2/1.3 by the str0m author), a new `crates/dtls` shim (role negotiation, fingerprint pin-verify, RFC 4.2 role-aware split for all three negotiable profiles, typed CM view as the only bind route), transport DTLS demux, per-session engine DTLS endpoints with kick/nomination/sweep-timer plumbing, RFC 5763-correct `a=setup` mirroring, and media-level ICE-cred fallback in offers (a genuine-browser gap found by the engine tests). Per-boot self-signed identity replaces the static answer fingerprint, with a loud WARN for the fixture-pin env override.

Gates: `cargo test --workspace` → **107 PASS / 0 FAIL** (90 pre-leg); `cargo fmt --check` clean; `cargo clippy --workspace --all-targets` **0/0**; `cargo build --release --workspace` OK; Go untouched (`-race` suite still all-ok); live `-tags it` lane against the real binary PASS with a per-boot identity fingerprint.

Documented adaptations (both registered in PROMPT2-DESIGN.md amendment (b)): (1) in-repo loopbacks necessarily negotiate AEAD-GCM — dimpl's own offer/preference pair makes CM unreachable crate-internally and a byte-level CH rewrite is correctly transcript-rejected — so split/export is proven profile-wide, while `aes128_cm()` at the bind gate enforces the engine's CM-only crypto loudly; (2) the browser-grade UA lane (pion/simplewebrtc + Chrome/Firefox/Safari SDP verification) remains the one open `it` item, matching the design doc's own rollout plan. `bind_srtp_split` direction mapping (inbound = client-write keys ⇔ subscriber decrypts engine-protected legs) is proven with real RTP in both directions.

Remaining Prompt-2 scope: dashboard wire-1.2 adoption (TS client work — out of this workspace's scope rules), the browser UA `it` lane above, fleet + staging verification (infra).

---

## Addendum — Prompt-2 leg 5: genuine pion-UA lane; CM DTLS-SRTP proven end-to-end; RFC 8489 STUN HMAC bug found & fixed

**Status: SHIPPED (2026-09-18).** Two real pion/webrtc v4 peer connections with CM-only SRTP policies ran ICE + DTLS + CM-SRTP media loopback through the spawned production engine binary, with media payload + SSRC lineage + metrics ledger verification.

**Highest-value bug of the whole Prompt-2 run:** engine STUN MESSAGE-INTEGRITY framed self-consistently (fixture-generated ↔ fixture-verified) but NOT per RFC 8489 §14.5 — every browser-class client was being 401'd during ICE connectivity checks pre-fix. Only a genuine third-party client on the wire could expose it; found via captured pion frames + reference HMAC recompute; fixed in `crates/webrtc/src/stun.rs` (signing + verifying), regressed permanently against a real captured pion request.

Also shipped leg content: additive optional `ssrc` on the publish frame (real-client SSRC attribution; collisions + ssrc 0 refused loudly; fixtures unchanged); engine `/metrics` DTLS outcome counters + publish-refused counter; sandbox-honest announce-IP selection in the lane; pion webrtc v4.2.20 / dtls v3.1.9 as it-tagged test-only deps.

**Gates (final state, all rerun):** Rust `cargo test --workspace` 109/0; fmt clean; clippy 0/0; release build OK. Go `-race` 18 ok / 0 fail; vet/gofmt clean; both `it` lanes PASS against the real engine (base 0.14s; pion 0.57s with per-boot fingerprint). Recorded in full in PROMPT2-DESIGN.md Amendment (c).
