# Prompt-2 Design — the two heavy items deferred from the completion pass
**Status:** implementation-ready specification (no code yet). Written 2026-09-17 after the Prompt-1 completion leg; referenced as PROMPT1-FINAL-REPORT.md §7 items 1–2.

---

## Item 1 — SFU steer: browser media actually flows through media-engine-rs

### Goal and non-goal
Today: gateway P2P relay carries offer/answer/candidate between the two members of a `session.*`; the Rust SFU tracks lifecycle (join/leave + RTP/RTCP processing) but browsers never address it. Goal: for a 2-member gateway signaling session, media runs **browser ⇄ engine ⇄ browser** (true SFU steer), with the P2P relay path remaining for sessions/tenants the operator has not migrated and as an explicit fallback (`engine steers=false` kill-switch per config).

### Wire: browser protocol v1.2 (additive; clients negotiate via `hello`)
- `hello` gains optional `"ws": 2` capability marker; the gateway's `ready` gains `"engine": {"steer": true,"session_prefix":...}` when v1.2 accepted AND engine up AND steer enabled.
- New client→server frames:
  - `engine.offer` `{session_id, sdp}` — the browser's SDP offer (RFC 8829 complete empty-offers allowed).
  - `engine.candidate` `{session_id, candidate|null}` — trickle, including e.o.c `null`.
  - `engine.publish` `{session_id, track, kind}` — reuse current publish vocabulary only when steer active.
  - `engine.subscribe` `{session_id, participant, track}` / `engine.unsubscribe`.
- New server→client frames: `engine.answer` `{session_id, sdp}`, `engine.track_published` `{session_id, participant, track, kind}`, `engine.ready` `{session_id(engine), participant}` (capability triple minus ICE creds — creds stay server-side; the browser's candidate harvesting is the only ICE surface it needs).
- Refusals continue down the existing `error` frame channel; codes reuse the gateway's approved taxonomy (`engine_unavailable`, `bad_message`, `room_unknown`, `over_limit`).

### Gateway wiring (target files, delta from Prompt-1 state)
- `internal/signaling/router.go` + new `internal/signaling/steer.go`:
  - v1.2 upgraded connections route `engine.*` frames through a NEW Router section; ordering with the member lifecycle is by the session manager's lock (same invariant as emit).
  - On `session.peer_joined` for a steered session: both members already have engine sessions (Prompt-1 join hook); steer mode just flips the offer route.
- `internal/engineclient/client.go`: add typed `Offer(ctx, session, sdp) ([]Frame, error)`, `Trickle`, `Publish`, `Subscribe` wrappers + op-bucket names for metrics (`offer`/`trickle`/`subscribe` counters live next to join/leave).
- Engine response fanout: server frames returned by the engine in the SAME HTTP response route back to the initiating connection (`engine.answer`, errors) and to the room's other member (`engine.track_published`) via a mapping engineRoom ⇄ gateway session kept on the router. **No engine-initiated push channel is built;** publish events reach the peer inside the peer's next poll... — see "Push problem" below.

### Engine deltas
- Preserve current behavior for `join/publish/subscribe/leave/trickle`. Offer handling ALREADY answers; **NO change needed.**
- `track.published` fanout: today it's a route-emit only. Extend HTTP control with one long-poll endpoint per gateway process (`GET /v1/events?room=…` is rejected: too many rooms per gateway on one HTTP line) — see decision below.

### The push problem (the one real design fork)
The engine has no push transport; events the gateway must pass to *the other* member (track.published) arrive only inside answers to that member's own calls. Options:
- **A1 — Event bus per room with single fan-out poll per gateway**: gateway maintains ONE long-lived SSE-ish stream per media engine node: `GET /v1/bus` (chunked JSON lines of ALL rooms' events), reconnecting with backoff. Simple, single point of reconnect, no polling. Engine implements a ring-ordered bus keyed on a monotonically increasing seq (idempotent resume from `?since=N`).
- **A2 — Piggyback only**: publish events ride the engine's "join fanout" inside the joining HTTP response header section. Cheaper; can delay publish visibility until the peer's next call — unacceptable for UX.
- **Decision: A1.** Rationale: one TCP stream per engine node per gateway replica (bounded by fleet size × replicas, not by sessions), full idempotent resume (`since` seq), graceful degradation (bus down → members still steer media; publish visibility delayed + logged + metric). Auth for the bus: the gateway↔engine link is compose-network-scoped; a shared token header is added nonetheless (both sides must be able to run on less trustful throats; header `X-Voxdesk-Bus-Token`, ?required in prod).

### Rollout
1. Steer OFF by default (`VOXDESK_GATEWAY_ENGINE_STEER=off|v1.2|force`).
2. `v1.2`: only upgraded clients + operator-accept tenants steer; P2P continues side by side.
3. `force`: all sessions steer; P2P fold deprecation follows in a later prompt.

### Tests to write (when implementing)
- Rust: offer batching per mid; `on_bus` ordering/resume; publish fanout ordering vs. subscribe leg admission.
- Go: steer envelope surfacing engine answers; bus reconnect/resume (fake engine emits gap); degrade cases (engine down during steer ⇒ session continues, frames refused cleanly); the 8 integration cases from Prompt-1 section H PLUS steer equivalents.
- Live: browser-free E2E with two ws clients + real engine bin (extend `it_live_test.go`).

## Item 2 — DTLS-SRTP key extraction (production browser ICE → SRTP)

### Reality check
Browsers REQUIRE DTLS+SRTP (RFC 8827); the engine currently accepts SRTP via `bind_srtp(master_key, master_salt)` — a hook intentionally written so key material arrives from ANY lawful source. Implementing a browser-grade DTLS 1.2 handshake, certificate verification, rekeying, and alert protocol from scratch is measured in thousands of lines and belongs in a reviewed crypto crate, NOT in this workspace's "std-only" self-imposed constraint.
### Decision (documented here so the later phase is unambiguous)
- The std-only constraint is **lifted for exactly one dependency tree**: `rustls-dtls`-style OR OpenSSL-SRTP-bindings — the gate is: maintainers-active, fuzzed, DTLS 1.2 + 1.3, RFC 5764 use_srtp extension, `SSL_get_selected_srtp_profile`-equivalent key-material export. Candidates at decision time: `webrtc` crate-family's `dtlss` (pure Rust, WebRTC-targeted, mature), or `openssl` crate with DTLS enabled.
- Handshakes terminate ON the engine: verifier role per offer's `a=setup`, fingerprint match against the SDP `a=fingerprint`, then `bind_srtp(exported_master_key, exported_master_salt)` per RFC 5764 extraction order (client keys to inbound, server keys to outbound).
- The current SDES-style path remains ONLY in test/fixtures, clearly named; it is never browser-visible.
- Effort estimate: ~2–3 days of focused integration + interop verification against Chrome/Firefox/Safari SDP; the certificate for the engine's self-signed identity is generated per boot (standard SFU practice), fingerprint pinned into answers as today.
- Tests: RFC-exported conformance vectors for key extraction at minimum; live round-trip against an SDP-legal test UA (simplewai/pion-based driver) in the `it` gate.

### Cross-cutting acceptance for both items
- Zero demo toggles: steer defaults OFF until the e2e evidence is in; DTLS is *required* for steer=force rollout.
- Gate discipline identical to Prompt 1: full `cargo fmt/clippy/test`, `gofmt/vet/-race`, live it-lane, honest report incl. anything the sandbox can't execute.

---

# Amendment 2026-09-17 — IMPLEMENTATION RECORD (Item 1: steer SHIPPED as specified except one fork revision)

## What was implemented
- **Browser wire 1.2**: hello `"ws":2` capability marker (pinned at hello alongside tenant identity); five client frames (`engine.offer/.candidate/.publish/.subscribe/.unsubscribe`) with codec presence rules; two server frames (`engine.answer`, `engine.track_published`); `session.joined`/`session.peer_joined` gained the always-present `steer` boolean (additive — golden wire tests updated deliberately for exactly these four pinned shapes plus the two new frames).
- **Negotiation** (`internal/signaling/router.go#negotiateSteer`): steer lands ONLY when mode != off AND engine monitor up AND (force | both members v1.2-marked). Force does NOT override an unavailable engine — that pairing degrades to P2P, because joining a steered session against a dead SFU hangs calls at the engine admission gate (documented in code).
- **Mode mixing is refused both directions** with `steer_mode_blocked` (`internal/signaling/{offer,answer,candidate}.go` + `steer.go`).
- **Steer handlers** (`internal/signaling/steer.go`): membership+enrolment gate (SessionForConn + engineSessions), payload guards mirroring the relay handlers, per-call 1.2s bound, engine failures → session-preserving `engine_unavailable` + classified logs; engine in-band errors map onto the CLOSED Go code vocabulary (engine codes never leak); remote-frame dispatch: `answer` → caller, `track.published` → peer, everything else logged+dropped (no blind relay).
- **Engine client**: typed `Offer/Trickle/Publish/Subscribe/Unsubscribe` wrappers.
- **Config**: `VOXDESK_GATEWAY_ENGINE_STEER=off|v1.2|force` (closed set; steer without engine URL = boot refusal), compose + .env.example + boot-log surface.
- **FORK REVISION (documenting honestly): the `/v1/bus` (design call A1) was NOT built.** During implementation it turned out every event the 2-member gateway model needs to reach the peer (track.published) already rides the same HTTP response that carries the caller's own pipeline acks — the Rust engine returns `reply + room_fanout` in one frame array (verified live in `it_live_test.go`: publish → real engine returns the `track.published` frame). A bus would move zero bytes the response path doesn't already deliver, so the response-fanout path shipped instead — same semantics, strictly smaller surface. The bus design is retained below for the multi-member-room feature line (3+ publishers per room, engine-side async events like sweep-expiry peer notification), where it becomes load-bearing again.
- **Rollout**: default off; v1.2 for upgraded pairs; force only after the dashboard ships 1.2.

## Gates at implementation end
- `go test -race -count=1 ./...`: **all 19 packages ok; 186 --- PASS / 0 FAIL** (was 177 before this leg).
- Live lane (`-tags it` against the real media-engine binary): PASS incl. publish-fanout assertion against real engine behavior.
- `go vet`, `gofmt -l`: clean. One test-side data race found by -race during development (scripted-engine list read without lock) — fixed; production paths race-clean.
- Rust: untouched this leg; 90/90 gate from the previous leg stands (steer needed ZERO engine changes — publish/offer/trickle/subscribe effects were already complete).
- Golden wire-format delta is deliberate and minimal: `steer` field on the two join acks + two new frame types.

---

# Amendment 2026-09-17 (b) — IMPLEMENTATION RECORD (Item 2: DTLS-SRTP termination SHIPPED, with two documented reality adaptations)

## Crate chosen & dependency-tree decision
- **`dimpl` =0.7.3, pinned exact** (algesten / str0m author; MIT OR Apache-2.0; `forbid(unsafe_code)`; Sans-IO **sync**, DTLS 1.2 + 1.3, RFC 5764 use_srtp export, actively maintained, WebRTC-aimed). It is the workspace's single allowed dependency tree; everything else remains hand-rolled.
- **Feature/entropy decision (documented):** dimpl's `default` features = aws-lc-rs crypto provider + rcgen cert generation — the author's audited default path; rcgen's P-256 generation is hard-coupled to aws-lc-rs upstream. The 100%-pure-RustCrypto alternative (`rust-crypto` feature) was evaluated and BUILD-VERIFIED in the sandbox, but loses cert generation entirely (no pure-Rust signing backend can generate the identity) — rejected. Consequence: the tree contains aws-lc_rs C bindings compiled with the system cc at build time (gcc 14 present in sandbox; multi-stage Docker builds likewise). If a zero-C build is ever required, the flag day is: switch feature set + supply the identity from out-of-tree key material.
- Self-signed ECDSA P-256 identity is generated **per boot** in `bins/media-engine/main.rs` (standard SFU practice); its SHA-256 fingerprint replaces the static `VOXDESK_DTLS_FINGERPRINT` label as the answer's `a=fingerprint`. The env var survives ONLY as a fixture pin (boot logs a loud WARN if it disagrees with the real identity, since browsers would otherwise fail the peer-cert check against the answer).

## What was built (files)
- **`crates/dtls`** (new, ours): role negotiation (passive default; active iff offer said `passive`), Sans-IO drive loop with buffer-grow + spin-cap, peer-cert SHA-256 fingerprint verification against the SDP pin BEFORE keys surface, RFC 5764 §4.2 role-aware split for ALL three profiles dimpl negotiates (CM 16/14/60B, GCM-128 16/12/56B, GCM-256 32/12/88B), and the typed `aes128_cm()` view that is the ONLY route into the engine's bind path.
- **transport demux**: RFC 7983 DTLS arm (byte0 20..64, ≥13B record header) — checked BEFORE STUN (content types 22..25 satisfy the STUN arm's loose mask; the bug was caught by the very first engine test).
- **signaling**: `OfferContext` now carries `setup` + `peer_fingerprint` (extracted from the offer: BUNDLE-first-media plus session fallback). ICE creds now ALSO fall back to media level — real browser SDP puts them there; before this leg the extraction read session level only, which would have yielded NO OfferContext/DtlsEndpoint at all against a genuine Unified-Plan offer (found and fixed during engine-level tests).
- **webrtc/sdp**: `build_answer` now mirrors RFC 5763 — `a=setup:passive` for actpass/active offers, `a=setup:active` for passive offers (previously hardcoded passive, which would hang a passive offerer).
- **engine**: `EngineConfig.dtls_identity` (None = documented fixture posture), per-session `dtls::Endpoint` spawned on offer with the settled role; `by_endpoint`-routed `on_dtls` in `media_step`; `kick_dtls` flushes the active-role ClientHello buffered pre-nomination onto the nominated 5-tuple; sweep cadence services DTLS retransmit timers; `bind_srtp` kept as the SDES-style fixture API (now delegating to the new directional `bind_srtp_split(session, CmKeys)`); stats counters `dtls_dropped/.established/.failed/.profile_refused`.
- **Fail-closed posture**: protocol error or fingerprint mismatch tears the association down and NEVER binds keys (`dtls_failed`); a successful handshake on a profile the engine crypto can't bind (non-CM) is surfaced as `dtls_profile_refused` with the association scrapped — the design's "only CM is bindable" rule, enforced at the boundary, never silent.

## Documented reality adaptations (why they exist, what's NOT done)
1. **The in-repo loopback cannot negotiate CM.** dimpl's client offer hardcodes `[GCM-256, GCM-128, CM]` and its server prefers GCM when offered — two dimpl instances ALWAYS land on GCM-256. A byte-level ClientHello rewrite was attempted and correctly rejected by the DTLS transcript signature (handshake transcripts include the CH). Resolution chosen: the shim exports/splits ANY negotiated profile correctly (`Negotiated`), and the ENGINE fails closed at the only bind gate (`Negotiated::aes128_cm()`), so CM negotiation — what browsers actually offer — lands identically at the same code, with the same split semantics (proven at engine-test and unit level), while the browser lane proves the production profile end-to-end.
2. **The live-`it`-lane browser round-trip (the design's planned pion/simplewebrtc UA + real Chrome/Firefox/Safari SDP verification) is NOT part of this leg and remains open** — it needs a browser-grade UA the sandbox doesn't have; the design doc already schedules it for rollout. Engine-side everything up to that UA's first handshake packet is implemented and gate-green.

## Gates at implementation end (exact numbers)
- `cargo test --workspace`: **107 passed / 0 failed** (90 pre-leg; +17 net: dtls shim 4, loopback 4, engine dtls 7, demux 1, sdp setup 1; a debug harness test was removed). Suite runs RTP/RTCP/STUN/SDP/SRTP/Pipeline flows unchanged.
- `cargo fmt --all -- --check`: CLEAN. `cargo clippy --workspace --all-targets`: **0 warnings, 0 errors**.
- `cargo build --release --workspace`: OK (1m39s, LTO profiles untouched).
- Go: `go test -race -count=1 ./...` → all 19 packages **ok** (unchanged from the steer leg; Go code not touched this leg); `go vet`/`gofmt -l` clean.
- Live `-tags it` lane against the REAL engine binary: **PASS**, now booting with a per-boot identity fingerprint emitted in the answer (verified in the lane's log: `dtls identity active (answer fingerprint D6:B8…)`).

## Still open for the next leg (honestly registered)
- Browser/pion-UA `it` lane (CM production profile end-to-end incl. Chrome/Firefox/Safari SDP interop).
- Optional AEAD-GCM support in `crates/webrtc/src/srtp.rs` (RFC 7714) — only if a browser ever phases CM out; today every WebRTC client offers AES128_CM_SHA1_80, and dimpl negotiates it against those offers (server picks from the client's list; CM is the only commonality with our crypto).
- `/v1/bus` for multi-member rooms (already registered by Amendment (a)).

---

## Amendment (c) — pion genuine-UA lane: CM DTLS-SRTP end-to-end PROVEN, and the interop bug only a real client could find

**Shipped 2026-09-18.** The remaining question after Amendment (b) — "does an honest, browser-shaped client actually interop?" — is now answered ON THE WIRE: two genuine pion/webrtc v4.2.20 peer connections (full-ICE agents with real ICE credentials, real DTLS clients, real SRTP crypto), both FORCED to offer exactly one protection profile (`AES_CM_128_HMAC_SHA1_80`), connect through the spawned REAL engine binary, exchange CM-decryptable media both directions, and fail assertions on any deviation.

### What was built

1. **`internal/engineclient/it_pion_test.go`** (`-tags it`, same `VOXDESK_IT_ENGINE` gates): alice (send) + bob (recv), CM-only client posture, browser-baseline MID header-extension registered, RFC 7943-compatible receiver (engine speaks the minimal-SDP SFU dialect — no per-leg `a=ssrc` rows — so undeclared-SSRC media is accepted deliberately, with SSRC lineage + payload equality asserted by hand to keep the laxity honest). Actually-verified invariants:
   - ICE connectivity (engine ICE-lite ↔ pion full agents, both sessions);
   - two DTLS handshakes, both CM (server cannot pick GCM — the clients' protection-profile list excludes it);
   - alice's RTP decrypts at the engine under HER RFC 5764 outbound split, routes, re-encrypts under bob's split;
   - bob's OnTrack delivers the frame: payload bytes bit-equal, SSRC == alice's engine-published SSRC;
   - ledger cross-check on the engine's `/metrics`: `voxdesk_media_dtls_total{established}` delta == 2, `profile_refused` delta == 0, `publish_refused == 0`.
2. **Wire v1.2 (additive): optional `ssrc` on `publish`** (`crates/protocol`, `signaling`, `engine`). The engine previously MINTED session SSRCs internally and never told the client — a genuine UA publishing its own RFC 3550 SSRC would have every packet dropped as unknown-ssrc. Present → engine binds that SSRC (collision or 0 → loud refusal + fanout retraction + `publish_refused` counter); absent → mint (fixtures untouched). go.mod now carries pion webrtc v4.2.20 + pion/dtls v3.1.9 (test-only adoption).
3. **Engine `/metrics`**: `voxdesk_media_dtls_total{established|failed|profile_refused}` and `voxdesk_media_publish_refused_total` — the pion lane's ledger assertions read these.
4. **`VOXDESK_STUN_DEBUG` diagnostic streams** (engine, opt-in): STUN parse-drops and 401 audit reasons (never credentials) — this leg would have been blind without them.

### THE find: self-consistent STUN HMAC (RFC 8489 §14.5), broken for every real client

The engine's own StunBuilder hashed `header(len-through-MI-value) + attrs + MESSAGE-INTEGRITY-attr-header` when signing/verifying. RFC 8489 §14.5 — and pion/stun's actual wire — hash only through the attribute PRECEDING MI (MI's own header excluded), with length patched to the end of the MI value. Both directions were *mutually self-consistent*, so every fixture test passed while **every browser-class client was 401'd at connectivity checks** (perpetual ICE "checking"). A pion-signed request captured off the wire, then a Python reference recompute against the engine's own credentials, pinned trial A (RFC framing) as the true MAC; fixture rewritten to a real captured pion frame (`pion_signed_binding_request_verifies_our_integrity`), signing+verifying corrected in `crates/webrtc/src/stun.rs`. Same-class fix Adam-style lessons: fixture-mutual-consistency is not wire truth.

### Collateral learnings baked into the lane (not hacked around)

- **Announce IP == packet source IP**: a genuine ICE agent discards success responses from an address that wasn't an advertised remote candidate ("no such remote") — same failure class as a mis-mapped NAT. The lane auto-selects the host's first non-loopback address (`VOXDESK_ANNOUNCE_IP` overrides) instead of papering over with the sandbox's loopback↔link-local quirk.
- pion puts ICE creds and the DTLS fingerprint at MEDIA level — the engine's existing media-level fallback (`signaling/lib.rs`, "BUNDLED browser SDP…") proved its worth; no change needed.
- pion does NOT register the MID header extension by default (browsers do); the UAs register it explicitly to stay in the browser set.

### Gates (all rerun after the final state)

- `cargo test --workspace`: **109 passed / 0 failed** (pre-leg 107; +engine publish-ssrc test, +pion-signed STUN regression; −1 debug scaffold removed).
- `cargo fmt --all -- --check`: clean. `cargo clippy --workspace --all-targets`: 0 warnings / 0 errors.
- `cargo build --release --workspace`: OK (22.8s; LTO profiles untouched).
- Go: `go test -race -count=1 ./...` → 18 ok / 0 fail (one fixed-package set; pion deps are it-tagged only). `go vet` (`-tags it` incl.) clean; `gofmt -l` clean.
- Live `-tags it`: base lane (`TestLiveEngineJoinOfferTrickleLeave`) PASS 0.14s; pion lane PASS **0.57s**, log line `pion lane: 10 CM-SRTP media frames verified end-to-end (alice ssrc=…)`.

### Still open for the next leg (honestly registered)

- Per-leg SDP fidelity: MID/SSRC rewriting so multiple media sections across different legs route without dialect concessions (today asserted with deliberately receiver-lax UAs on a single-media-section shape — honest, but not yet the full SFU picture).
- Optional AEAD-GCM in `crates/webrtc/src/srtp.rs` (RFC 7714) if CM ever leaves the browser set.
- Real-browser lane (Chrome via WebDriver) — the pion lane proves the production-profile crypto/ICE cases; the browser lane would re-prove the same SDP-shape claims against libwebrtc.
- `/v1/bus` for multi-member rooms (Amendment (a)).
