# Realtime events (dashboard live updates)

VoxDesk pushes **notices about calls** to the dashboard over a durable
WebSocket: a call was created, a call changed state, a transfer resolved.
This document is the whole story — what moves, over which wire, with which
guarantees, and what to set where.

## The three moving parts

```
┌────────────┐  POST /ingest/v1/publish   ┌──────────────────┐  WS /realtime/ws   ┌────────────┐
│  API (this │ ─────────────────────────► │ realtime-gateway │ ─────────────────► │ dashboard  │
│  repo,     │  Bearer ingest secret      │ (Go,             │  hello {token:JWT} │ (realtime. │
│  app/      │  after every durable       │  services/       │  then subscribe    │  js)       │
│  realtime/ │  call fact commits         │  realtime/       │  frames)           │            │
└────────────┘                            │  gateway-go/)    │                    └────────────┘
                                          └──────────────────┘
```

1. **Publisher** — `app/realtime/`. Emits one event per *logical change*,
   after the database commit that made it real. Never raises, never blocks
   the webhook response path, and stays silent when unconfigured.
2. **Gateway** — `services/realtime/gateway-go/`. A standalone Go service:
   authenticates dashboard browsers against the same JWT the API issues,
   validates ingest payloads against a closed room/kind vocabulary,
   suppresses replays, fans out to subscribers. It knows nothing about
   calls — the payload is an opaque envelope.
3. **Client** — `dashboard/src/lib/realtime.js`. Keeps one socket up,
   rotates the token when the gateway says so (close `1008`), replaying
   subscriptions after every reconnect.

## Event kinds and the payload contract

| kind           | emitted when                                              | extra fields        |
| -------------- | --------------------------------------------------------- | ------------------- |
| `call.created` | `/telephony/voice` created a genuinely new call row       | —                   |
| `call.updated` | `/telephony/status` *applied* a state change              | —                   |
| `call.transfer`| the `<Dial>` callback *changed* the transfer state        | `transfer_outcome`  |

The payload is **routing + display facts only**: `call_id`, `call_sid`,
`status`, `direction`, `duration_seconds`, `booked`, `escalated`,
`lead_score`, `transfer_state`, `started_at`, `ended_at`. Deliberately
absent — and asserted absent by `tests/test_realtime_publisher.py`: phone
numbers, summary, transcript text, recording URLs. A realtime frame learns
*that* a call changed; the detail views re-read the content through the
authenticated REST API. Treat every frame as "refetch signal with enough
metadata to animate a list."

## Rooms

| room           | who subscribes                        | receives                       |
| -------------- | ------------------------------------- | ------------------------------ |
| `calls`        | wallboards, call list pages           | every call event for the tenant|
| `call:<uuid>`  | a call detail page                    | events for that one call only  |

The room namespace is closed and validated at the gateway (`calls`,
`metrics`, `call:<uuid>`, `campaign:<uuid>`); an arbitrary client-declared
room is refused with a 422-class error. Each logical event is published to
both rooms independently, with **different event ids per room** — that is
intentional, so a partial publish (one room delivered, the other refused by
a transient error) stays retryable for the missed room.

## Exactly-once, as far as it goes

* **DB → gateway:** the API derives `event_id = uuid5(namespace,
  "<call_id>:<kind>:<room_scope>:<status>:<transfer_state>")`. The id is
  deterministic, so a crash between commit and publish — replayed on boot or
  by an operator — is dropped by the gateway's (tenant, event_id) replay
  cache (TTL 30 min, 50k entries).
* **Webhooks → DB:** duplicates never produce an event. A retried Twilio
  callback fails `result.applied` / `mark_transfer_*`'s changed-flag, which
  is the same flag that gates emission. `tests/test_realtime_wiring.py`
  pins this: two identical callbacks, exactly one frame.
* **Gateway → dashboard:** at-most-once per connection. A dropped socket may
  miss frames; the UI is expected to resync details through REST on reconnect
  (the client's `ready` → resubscribe moment is a good trigger).

## Configuration

### API (`app/core/config.py`)

| env                                | default           | meaning                                        |
| ---------------------------------- | ----------------- | ---------------------------------------------- |
| `REALTIME_GATEWAY_URL`             | *(empty = off)*   | gateway base URL; the API appends `/ingest/v1/publish` |
| `REALTIME_GATEWAY_INGEST_SECRET`   | *(empty = off)*   | shared Bearer secret for ingest                |
| `REALTIME_PUBLISH_TIMEOUT_SECONDS` | `1.5`             | hard per-publish timeout                       |

Either field without the other is a **startup refusal** in production
(half-configured realtime fails more confusingly than none). Placeholder or
`< 16`-char secrets and non-http(s) URLs are refused too. With both empty,
the feature is cleanly off.

### Gateway (excerpt — full table in the gateway README)

| env                               | meaning                                             |
| --------------------------------- | --------------------------------------------------- |
| `VOXDESK_GATEWAY_JWT_SECRET`      | **same secret the API signs access tokens with**    |
| `VOXDESK_GATEWAY_JWT_ISSUER` / `…_AUDIENCE` | must match the API's `JWT_ISSUER`/`JWT_AUDIENCE` |
| `VOXDESK_GATEWAY_INGEST_SECRET`   | must equal the API's `REALTIME_GATEWAY_INGEST_SECRET` |
| `VOXDESK_GATEWAY_ALLOWED_ORIGINS` | exact browser origins allowed to open `/ws`         |
| `VOXDESK_GATEWAY_METRICS_TOKEN`   | Bearer for `/metrics`; empty = ungated (warns)      |

### Deploy wiring (already in this repo)

* `docker-compose.prod.yml` — `realtime-gateway` service (host port
  loopback-only, `127.0.0.1:8790`), and the API gets
  `REALTIME_GATEWAY_URL=http://realtime-gateway:8790` plus the required
  `${REALTIME_GATEWAY_INGEST_SECRET:?}` interpolation, so a missing secret
  fails `compose up` before anything boots half-configured.
* `Caddyfile` — one public route: `handle /realtime/ws` → rewrite to `/ws` →
  `realtime-gateway:8790`. Ingest, metrics, and health endpoints have **no**
  public path. Caddy passes the WebSocket upgrade through unchanged.
* `observability/prometheus.yml` — `voxdesk-realtime-gateway` job. When
  `METRICS_TOKEN` is set (the standard production posture), uncomment the
  `authorization.credentials` line with the same token; otherwise the target
  401s, which is at least loud.
* `dashboard/vite.config.js` — dev proxy for `/realtime/ws` →
  `localhost:8790` with the same `/ws` rewrite, so dev and prod see the
  identical wire.

## Wire protocol (summary)

Client → gateway: `{"type":"hello","token":"<JWT>"}` must be first
(`ping` is tolerated before it), then `{"type":"subscribe","room":"calls"}`,
`{"type":"unsubscribe",…}`, `{"type":"ping"}`.

Gateway → client: `welcome` (timeouts), `ready` (tenant bound, expires-at),
`subscribed`/`unsubscribed`, `delivery {room, kind, payload, event_id,
sent_at}`, `error {code, message}`, `pong`. Auth problems end as close code
**1008** with reason `token_expired` or `authentication failed` — the JS
client treats that pair as "rotate once, reconnect immediately" and anything
else as backoff. See `internal/protocol/protocol.go` for the normative
definition.

## Failure semantics

| failure                          | what happens                                                        |
| -------------------------------- | ------------------------------------------------------------------- |
| gateway down / unreachable       | publishes return `False` and log `realtime.publish_failed` (type only, never the secret); webhooks respond exactly as before; dashboards reconnect on backoff |
| gateway 401s ingest              | `realtime.publish_rejected` with the status — means the two secrets differ |
| dashboard token expires mid-socket | gateway closes 1008/`token_expired`; client refreshes once and comes back immediately |
| API session truly over           | subscription attempts stop after one failed refresh; the next REST 401 drives the login flow |
| publish path throws anything     | logged, swallowed — realtime must never 500 a telephony webhook     |

## Scoped honestly

* Fan-out is **single-process**: all dashboards must reach the same gateway
  instance (sticky-less horizontal scaling is future work; capacity ceilings
  are in the gateway README).
* Token **revocation** (`token_version`) is checked on REST calls, not per
  WebSocket frame: a socket lives until the JWT's own `exp`, at which point
  the gateway closes it. Maximum exposure equals the access-token lifetime
  (15 minutes by default).
* Events are ephemeral — no event store, no catch-up. Reconnecting pages
  resync through REST.

### A second traffic class rides the same socket: signaling

The gateway ALSO speaks a signaling plane (WebRTC session setup:
`session.start/join/end`, SDP offer/answer and ICE candidate relays between
two same-tenant connections). It shares the authentication, the connection
caps and the socket lifecycle with the notice plane, but it is NOT part of
the event pipeline described above: nothing on it is published from the API,
and its frames never flow through `app/realtime/`. The dashboard client in
`src/lib/realtime.js` handles only notice-plane frames; signaling frames
would arrive as ignored unknown types until a consumer adds dispatching.
See the gateway README's signaling tables for the wire vocabulary and
guarantees (glare guard, capability session ids, collapsed unknowns).

## Verifying it locally

```bash
# 1. gateway tests
cd services/realtime/gateway-go && go test -race ./...

# 2. publisher + wiring tests (API side)
/usr/local/bin/python3 -m pytest tests/test_realtime_publisher.py tests/test_realtime_wiring.py -q

# 3. dashboard client tests
cd dashboard && npx vitest run tests/realtime.test.js
```

For a live loop: run the gateway with dev secrets, set `REALTIME_GATEWAY_URL`
+ `REALTIME_GATEWAY_INGEST_SECRET` in `.env`, run the API and dashboard, and
watch the dashboard's network tab hold one `wss://…/realtime/ws` while calls
arrive from Twilio's test console.
