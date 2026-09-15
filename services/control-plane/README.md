# services/control-plane — Rust control plane (roadmap Phase 2)

The control plane owns the *coordination* half of a call: session lifecycle,
SIP/SDP signaling, WebSocket fan-out to live clients, and the usage/rating
pipeline. It is a Cargo workspace split so the invariants that must never
depend on a runtime are testable with `std` alone.

## Workspace layout

```
control-plane/
├── Cargo.toml               workspace manifest (resolver = "2")
└── crates/
    ├── core/   voxdesk-control   — std-only invariants (no runtime deps)
    │   ├── session.rs       call + transfer state machine (mirrors
    │   │                    app.db.models CallStatus / TransferState names)
    │   ├── registry.rs      tenant-scoped concurrent registry
    │   ├── idempotency.rs   exactly-once guard for cross-service events
    │   ├── broadcast.rs     bounded fan-out with backpressure-drop
    │   ├── scheduler.rs     interval scheduler (mirrors scripts/scheduler.py)
    │   ├── usage.rs         metering + rating (mirrors app/billing/metering.py
    │   │                    and app/billing/plans.py)
    │   ├── ratelimit.rs     token-bucket rate limiter (logical clock)
    │   └── retry.rs         exponential-backoff policy for webhook fan-out
    └── signal/ voxdesk-signal    — tokio + tungstenite WebSocket hub
        ├── protocol.rs      tagged-JSON client/server messages
        ├── hub.rs           tenant-scoped rooms, delivery, reaping
        ├── main.rs          `signal-server` binary (port 8765)
        └── tests/hub.rs     integration tests over a real TCP socket
```

## Why `core` is dependency-free

The rules this service must never violate — tenant isolation, exactly-once
side effects, a bounded fan-out that a slow client cannot stall, and a call
lifecycle whose vocabulary matches the database enums — are enforced as plain
unit tests before any network layer is added. `voxdesk-control` compiles and
tests without pulling a single dependency, which keeps those invariants
auditable and cheap to run in CI.

## State machine vocabulary

`session.rs` uses the same enum member *names* as the Python models
(`CallStatus`: `Ringing/InProgress/Completed/Failed/NoAnswer/Transferred`;
`TransferState`: `None/Requested/Dialing/Connected/Failed`) because SQLAlchemy
persists member names, not values. A Rust shadow of a call can therefore be
compared member-for-member with the database during the shadow-mode parity
checks described in `docs/EXPANSION-ROADMAP.md`.

Enforced transitions (verbatim from `app/telephony/call_state.py`):

- Calls: `Ringing -> InProgress | NoAnswer | Failed | Completed`;
  `InProgress -> Transferred | Completed | Failed`;
  `Transferred -> Completed | Failed`. Terminal states are exactly
  `{Completed, Failed, NoAnswer}` and never move; `Transferred` is **not**
  terminal — the call is still up with a human on it.
- Transfers (from `app/telephony/transfer_service.py`): intent is recorded as
  `Requested`; the provider accepting the redirect moves it to `Dialing`, and
  only then does the call flip to `Transferred`. `Connected` arrives only
  from a provider callback, never from the AI. `Requested | Dialing |
  Connected` is the idempotency lock — a repeated request while in flight
  returns the existing state and never dials twice. A failure recorded with
  the `inferred: ` prefix may be corrected by a later `Connected` callback;
  provider-reported failures never are, and a late failure after `Connected`
  is ignored.

## Scheduler, metering and the Python shadow

The roadmap's Phase 2 exit is parity: shadow-mode runs where the Rust path
reproduces the Python path on the same inputs. The pieces added here are the
ones with a concrete Python counterpart to be shadowed:

* `scheduler.rs` mirrors `scripts/scheduler.py` — the same seven named jobs
  (reminders 120s, campaigns 60s, knowledge 15s, CRM sync 20s, billing
  reconciliation 3600s, retention 86400s, stuck sweep 60s) with the same
  invariant that a failing tick never stops the loop.
* `usage.rs` mirrors `app/billing/metering.py` + `plans.py` — append-only
  idempotency-keyed events, integer smallest units, voice overage rounded up
  to whole minutes once on the period total, and the exact seed plan
  catalogue (trial / starter / pro / enterprise with the same included units
  and millicent rates).

## Signaling protocol

Clients connect over WebSocket and speak tagged-JSON (see `protocol.rs`). A
connection must send `hello {tenant_id}` before anything else; every room is
keyed by `(tenant_id, room)`, so cross-tenant delivery is structurally
impossible. Delivery is non-blocking backpressure-drop, and idle sockets are
reaped after 60 s.

## Building and testing

```sh
# Requires Rust 1.98+ (rustup stable).
cargo fmt --all --check      # formatting
cargo clippy --workspace --all-targets   # lints
cargo test --workspace       # core unit tests + signal integration tests
cargo run -p voxdesk-signal --bin signal-server   # dev server on :8765
```

`VOXDESK_SIGNAL_PORT` overrides the listen port. The server binds
`0.0.0.0:<port>` and serves until terminated.
