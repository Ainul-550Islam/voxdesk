# realtime/gateway-go — Public WebSocket Edge

The browser-facing realtime front door. One WebSocket carries TWO planes for
authenticated dashboard clients:

1. **The notice plane** (what the API publishes): call status changes,
   wallboard metrics — pub/sub over tenant-pinned rooms, fed by
   `POST /ingest/v1/publish`.
2. **The signaling plane** (what clients publish to each other): point-to-
   point WebRTC session setup — SDP offers/answers and ICE candidates relayed
   between two connections of the same tenant, behind the same JWT edge.

```
dashboard (browser)                gateway-go (this)                 VoxDesk API (Python)
        │   wss GET /ws  ─────────▶  upgrade + capacity gate               │
        │   {hello: token} ───────▶  verify HS256 JWT (shared secret)      │
        │   ◀────── ready ────────   tenant pinned from token `tid`        │
        │   {subscribe: calls} ───▶  join (tenant, "calls") room           │
        │   ◀──── delivery ───────   fan-out ◀── POST /ingest/v1/publish ──┤
        │                                                                    │
        │   {session.start} ──────▶  registry seats initiator              │
        │   {session.join: id} ───▶  responder seated, initiator woken     │
        │   {offer|answer|candidate} ──▶ relayed to the peer, byte-verbatim │
```

## Why a separate service (and not a FastAPI WebSocket)

* **Blast radius.** The public edge holds tens of thousands of idle browser
  sockets; the Python API holds the telephony path. A socket flood must be
  able to kill the edge without touching a single live phone call — the
  same "the scheduler is a separate process" reasoning the API already uses.
* **Different trust posture.** The internal hub (`services/signal-go`)
  trusts its network; this edge trusts nothing: browsers authenticate, room
  names are a closed set, every limit fails closed.
* **Same protocol.** The wire vocabulary mirrors `voxdesk-signal` /
  `signal-go` (hello/subscribe/unsubscribe/ping →
  welcome/subscribed/unsubscribed/delivery/error/pong), with two public-edge
  additions: `hello` carries a **verified access token**, and `ready`
  acknowledges the pin. A client library written for the internal hub needs
  only the auth frame added.

## Security model (what is enforced, and where)

| Guarantee | Mechanism | Go file |
|---|---|---|
| Only dashboard users connect | HS256 JWT verification — **same token the API mints** (`app/auth/jwt.py`): alg pinned (no alg-confusion), iss/aud exact, `typ=access`, exp/nbf enforced, `sub`+`tid` must be UUIDs | `internal/auth/jwt.go` |
| Tenant isolation is structural | Rooms keyed `(tenant_id, room)`; tenant comes ONLY from the token — the client frame schema has no tenant field | `internal/hub/hub.go` |
| No room enumeration | Closed room namespace: `calls`, `metrics`, `call:<uuid>`, `campaign:<uuid>` | `internal/validate` |
| The edge never outlives the credential | Connection closed (1008 `token_expired`) ~30 s after the token's `exp`; dashboard reconnects with the token it already rotates | `internal/websocket/heartbeat.go` |
| Backpressure, never a stalled room | Bounded per-connection queues; full → drop + counted (Rust `BoundedBroadcast` contract), one named policy everywhere | `internal/backpressure/queue.go` |
| Floods | Global upgrade cap (pre-auth 503), per-tenant connection cap, per-connection frame rate limit, max message size, auth timeout | `server.go`, `reader.go` |
| Ingest can't be forged | Shared Bearer secret, constant-time compare (hashed, so length can't leak) | `server/ingest.go` |
| Replay suppression | `(tenant_id, event_id)` TTL cache; duplicate → `200 {"duplicate":true}` — retries converge, mirrors `MessageWebhookReceipt` | `server/ingest.go` |
| Boot fails closed | Missing/placeholder `JWT_SECRET` or `INGEST_SECRET` → process refuses to start (like `validate_security()`) | `internal/config/config.go` |
| Metrics not public | `METRICS_TOKEN` Bearer gate, constant-time | `server/metrics.go` |

### Signaling plane, same table continued

| Guarantee | Mechanism | Go file |
|---|---|---|
| Sessions never cross tenants | Session keyed by id but VALIDATED against the token's tenant pin; wrong tenant, wrong id and non-member all collapse to one `session_unknown` | `internal/session/manager.go` |
| Session ids are capabilities, not invitations | UUIDv4 (122 bits) + same-tenant requirement; no listing, no guessable sequence | `internal/session/session.go` |
| Exactly two seats, `identity` is the socket | Members are connections, not users — user/device identity never enters the ledger | `internal/session/session.go` |
| Glare is unreachable | Offering state admits exactly one outstanding offer; a second is `wrong_signal_state`, whoever sends it (Perfect Negotiation enforced once for all clients) | `internal/session/manager.go` |
| A dead socket never leaves a live session | Every close path (peer, policy, heartbeat, write failure) ends the session and tells the survivor `peer_disconnected` | `internal/websocket/connection.go` teardown |
| No negotiation stockpiles | A never-joined session is reaped after the pending TTL (`join_timeout`) | `internal/session/lifecycle.go` |
| Payloads are sized, never parsed | SDP ≤ 12 KiB with an `v=` sanity check, candidates ≤ 1 KiB; contents are relayed byte-verbatim — munging is media-plane territory, and a parsing edge would be one more thing to disagree about | `internal/protocol/codec.go` |
| One session per connection, 64 per tenant | `VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT`, plus the standing per-tenant connection cap | `internal/config/config.go` |

## Wire frames

Client → server (notice plane):
```json
{"type":"hello","token":"<dashboard access token>"}
{"type":"subscribe","room":"calls"}
{"type":"unsubscribe","room":"calls"}
{"type":"ping"}
```

Client → server (signaling plane; after `ready` only):
```json
{"type":"session.start"}
{"type":"session.join","session_id":"<uuid>"}
{"type":"offer","session_id":"<uuid>","sdp":"v=0 …"}
{"type":"answer","session_id":"<uuid>","sdp":"v=0 …"}
{"type":"candidate","session_id":"<uuid>","candidate":{RTCIceCandidate} | null}
{"type":"session.end","session_id":"<uuid>"}
```

Server → client (notice plane):
```json
{"type":"welcome","session_id":"…","auth_required":true,"auth_timeout_secs":10,"heartbeat_secs":20,"server_time":"…"}
{"type":"ready","session_id":"…","tenant_id":"…","role":"owner","token_expires_at":"…"}
{"type":"subscribed","room":"calls","peers":2}
{"type":"delivery","room":"calls","kind":"call.updated","payload":{…},"event_id":"…","sent_at":"…"}
{"type":"pong","server_time":"…"}
```

Server → client (signaling plane):
```json
{"type":"session.started","session_id":"…","role":"initiator"}
{"type":"session.joined","session_id":"…","role":"responder"}
{"type":"session.peer_joined","session_id":"…","peer_role":"responder"}
{"type":"signal.offer","session_id":"…","sdp":"v=0 …"}
{"type":"signal.answer","session_id":"…","sdp":"v=0 …"}
{"type":"signal.candidate","session_id":"…","candidate":{…} | null}
{"type":"session.ended","session_id":"…","reason":"member_ended|peer_disconnected|join_timeout"}
```

Either plane:
```json
{"type":"error","code":"hello_required|auth_failed|room_invalid|over_limit|rate_limited|bad_message|session_unknown|session_full|session_not_ready|already_in_session|wrong_signal_state|signal_too_large","message":"…"}
```

Ingest (API → gateway), `POST /ingest/v1/publish`,
`Authorization: Bearer $VOXDESK_GATEWAY_INGEST_SECRET`:
```json
{"tenant_id":"<uuid>","room":"calls","kind":"call.updated","payload":{…},"event_id":"<uuid, optional>"}
```
→ `200 {"delivered":n,"dropped":m,"duplicate":false}`

## Configuration (env; boot refuses invalid)

| Variable | Default | Notes |
|---|---|---|
| `VOXDESK_GATEWAY_PORT` | `8790` | |
| `VOXDESK_GATEWAY_JWT_SECRET` | — | **Required.** Must equal the API's `JWT_SECRET`. ≥32 chars, placeholder-rejected |
| `VOXDESK_GATEWAY_JWT_ISSUER` / `_AUDIENCE` | `voxdesk` / `voxdesk-api` | Must equal the API's values |
| `VOXDESK_GATEWAY_INGEST_SECRET` | — | **Required.** ≥16 chars, placeholder-rejected |
| `VOXDESK_GATEWAY_ALLOWED_ORIGINS` | empty | Comma-separated; empty = only same-origin/non-browser |
| `VOXDESK_GATEWAY_METRICS_TOKEN` | empty (warns) | Bearer gate for `/metrics` |
| `VOXDESK_GATEWAY_MAX_CONNECTIONS` | `10000` | Global, enforced pre-upgrade |
| `VOXDESK_GATEWAY_MAX_CONNS_PER_TENANT` | `256` | Per-tenant, enforced at hello |
| `VOXDESK_GATEWAY_AUTH_TIMEOUT` / `PONG_TIMEOUT` / `PING_INTERVAL` / `WRITE_WAIT` | `10s` / `60s` / `20s` / `10s` | `PONG_TIMEOUT` must exceed `PING_INTERVAL` |
| `VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT` | `64` | Concurrent signaling sessions per tenant; boot refuses `< 1` |
| `VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT` | `60s` | A never-joined session is reaped after this; boot refuses `≤ 0` |
| `VOXDESK_GATEWAY_BROKER` | `memory` | Ingest fan-out transport: `memory` (single node) or `redis` (multi-replica pub/sub). Unknown values **refuse boot** — a typo must never silently single-node a multi-replica deploy |
| `VOXDESK_GATEWAY_REDIS_URL` | empty | `redis://[user:pass@]host:port[/db]`, required when BROKER=redis; `rediss://` refused (no broker TLS on this edge today) |
| `VOXDESK_GATEWAY_LOG_LEVEL` | `info` | `debug`/`info`/`warn`/`error`; unknown values fall back to `info` (silence is never the default) |

The per-connection frame limiter (20 msg/s, burst 40), subscription cap (32),
message size (16 KiB), ingest payload (64 KiB), replay TTL (30 min, 50k
entries), SDP cap (12 KiB) and candidate cap (1 KiB) are compile-time
defaults in `internal/config/config.go` and `internal/protocol/codec.go`.

## Package map

```
cmd/gateway/main.go          — boot, wiring (observability logger, event
                               emitter, shutdown package driving graceful exit)
internal/config/             — env → validated config, fail-closed secrets
internal/protocol/           — wire vocabulary, one concern per file:
                               message.go (types + server frames),
                               envelope.go (client frame),
                               error.go (closed code set + close codes),
                               codec.go (decode + payload guards)
internal/auth/               — HS256 JWT split by responsibility:
                               jwt.go (envelope: algorithm pin, signature),
                               claims.go (payload → verified Claims),
                               middleware.go (ExtractBearer + constant-time
                               token equality — every secret comparison on
                               this edge goes through one implementation)
internal/validate/           — room/tenant/uuid shape predicates
internal/idempotency/        — ingest replay store (TTL + capacity, atomic
                               seen-before, tenant-scoped keys)
internal/ratelimit/          — policy → token bucket → Limiter; owns the
                               per-connection inbound frame budget
internal/backpressure/       — generic bounded Queue[T] + named drop policy
                               (DropNewest, counted) behind every outgoing
                               connection queue
internal/hub/                — (tenant, room) registry + fan-out + Lookup
internal/broker/             — ingest fan-out transport:
                               broker.go (Broker contract, Envelope, Stats),
                               memory.go (default: Publish IS the local
                               fan-out, synchronously, exact counters),
                               redis.go (multi-replica: hand-rolled RESP
                               client, AUTH/SELECT/SUBSCRIBE/PUBLISH, two
                               connections, supervised reconnect with
                               backoff, origin-stamp own-echo suppression),
                               brokertest/ (fake RESP Redis for tests)
internal/presence/           — presence/registry/events: tenant → online
                               users, keyed ONLY on verified JWT identity;
                               transitions feed the Prometheus gauge via
                               OnChange subscription
internal/observability/      — logging.go (leveled, kv-tagged logger),
                               tracing.go (X-Request-ID middleware),
                               events.go (structured events + emitters),
                               metrics/ (atomic counters/gauges +
                               Prometheus text exposition)
internal/session/            — signaling session ledger, one concern per file:
                               manager.go (operations → Events),
                               session.go (two-seat model),
                               state.go (Pending/Offering/Answered/Ended),
                               registry.go (id/tenant/conn indexes),
                               lifecycle.go (pending reaper)
internal/signaling/          — relay between two sockets of one session:
                               router.go (dispatch + teardown + reaper loop),
                               offer.go / answer.go / candidate.go (guards),
                               events.go (session events ⇢ frames, error codes ⇢ wire)
internal/shutdown/           — graceful.go: signal contract (INT/TERM,
                               buffered, restorable) + bounded http.Server
                               drain
internal/websocket/          — upgrade, connection, reader, writer,
                               heartbeat, close
internal/server/             — HTTP mux: /ws, /ingest/v1/publish,
                               /healthz, /readyz, /metrics; ingest publishes
                               through the broker (redis mode e2e-tested
                               across two full gateway instances)
tests/                       — cross-package tests (see tests/README.md):
                               protocol goldens, integration seams,
                               load smoke (behind -tags load)
```

Layer rule: `session` knows no frames, `signaling` knows no sockets,
`websocket` knows no session state machine, `protocol` knows no behaviours,
`broker` knows no event schema, `presence` knows no transport. Every arrow
points one way.

### Why there is no `pkg/`

The expanded tree calls for `pkg/` (types/errors/constants). Deliberately
not created: every type that LOOKS shared already has exactly one honest
owner — wire shapes in `internal/protocol`, close codes and error codes in
`internal/protocol/error.go`, the fan-out accounting in `internal/broker`'s
`Stats`. There is no exported API surface a `pkg/` would serve (this module
is one deployable, imported by nobody), and Go-idiomatic move-to-`pkg`
signals (a second in-repo consumer, an external import request) do not
exist yet. The moment the media engine or an SDK needs these types, the
types to lift are obvious and the lift is mechanical; an empty `pkg/` with
aspirational `types.go` files is how "shared" becomes "blamed".

## Run / verify

```sh
go build ./cmd/gateway
VOXDESK_GATEWAY_JWT_SECRET=<same as API> \
VOXDESK_GATEWAY_INGEST_SECRET=<shared>   \
./gateway

# gate (same shape as the other services):
gofmt -l .        # empty
go vet ./...
go test -race ./...   # 149 tests: full-socket round trips for BOTH planes,
                      # wire-level tenant isolation, the state machine's
                      # glare/connectivity matrix, every new package's unit
                      # suite, broker-over-fake-redis (echo suppression,
                      # outage, reconnect re-subscribe), and TWO full
                      # gateway replicas fanning one ingest out over the bus

# load smoke (off the default gate, behind its build tag):
go test -tags load ./tests/load/ -v   # 200 conns × 50 events, ~147k frames/s
```

## Scope honesty

* Token **revocation** (`User.token_version`) is not — and cannot be — checked
  here without a database; the enforcement is the token's 15-minute `exp`,
  same trust window the dashboard UI itself holds.
* Fan-out defaults to single-node (`VOXDESK_GATEWAY_BROKER=memory`, like
  the Rust hub and signal hub). Multi-replica fan-out now EXISTS behind
  `=redis`: envelope-carrying pub/sub with own-echo suppression and
  sync-local + best-effort-remote semantics, e2e-tested across two full
  gateway instances in `internal/server/redis_e2e_test.go`. What it
  deliberately is NOT: guaranteed/at-least-once cross-node delivery — a
  bus outage drops remote hops (counted in `BusDrops`, warned in logs),
  because a realtime event five seconds late is worthless and a spool
  would only pretend otherwise. Signaling members still must share a node
  or gain a session-affinity rule; presence remains node-local by design
  (multi-node "is U online anywhere" composes from node views, it is not
  faked here).
* There is **no "Connected" state** in the signaling session: the server can
  observe SDP ordering, it cannot observe media, so signaling correctness
  ends at Answered. ICE-health is the peers' problem (and the future media
  plane's telemetry, not this edge's ledger).
