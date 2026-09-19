# signal-go — Go real-time signaling hub

The Go half of the roadmap's **Rust/Go real-time WebSockets & concurrency
engine** increment. It is a **differential counterpart** of the Rust
`voxdesk-signal` crate (`services/control-plane/crates/signal`): same
tagged-JSON wire protocol, same tenant-scoping rules, same backpressure and
idle-reap behaviour — so a client written against one server works against
the other unchanged.

## Layout

```
signal-go/
├── go.mod                       module github.com/voxdesk/signal-go (Go 1.27)
├── cmd/signal-server/main.go    the `signal-server` binary (port 8765)
└── internal/signal/
    ├── protocol.go              tagged-JSON client/server frames + strict decode
    ├── uuid.go                  RFC 4122 v4 session ids (CSPRNG)
    ├── hub.go                   (tenant_id, room)-keyed registry + fan-out
    ├── server.go                per-connection reader/writer goroutines
    ├── ratelimit.go             token bucket (mirrors core/ratelimit.rs)
    ├── idempotency.go           exactly-once guard (mirrors core/idempotency.rs)
    └── *_test.go                unit + end-to-end tests over real TCP
```

## Concurrency model

One connection is exactly **two goroutines** plus shared hub state:

* **reader** — reads frames, enforces the idle deadline, dispatches to the hub;
* **writer** — drains the session's buffered outbound channel onto the socket.

The hub is a single mutex over two maps (`rooms` keyed by `(tenant_id, room)`,
`sessions` keyed by session id). Outbound queues are fixed-size channels
(64) with **non-blocking sends**: a slow peer is skipped, never a stall. A
writer whose socket dies closes the connection, which unblocks the reader and
ends the whole session — the same single-reaper shape as the Rust
`select!` loop. Outgoing channels are never closed; a removed session's
channel is garbage-collected, so a racing publish can never panic.

## Protocol

Clients send `{"type": "…", …}` and receive the same shape back:

| client → | server → |
|---|---|
| `hello {tenant_id}` | `welcome {session_id}` |
| `subscribe {room}` | `subscribed {room, peers}` |
| `unsubscribe {room}` | `unsubscribed {room}` |
| `publish {room, payload}` | `delivery {room, from, payload}` |
| `ping` | `pong`, `error {code, message}` |

Rules (identical to the Rust hub):

* `hello` must be the first message after `welcome`, exactly once, with a
  non-empty `tenant_id`; anything else is `hello_invalid`.
* `subscribe`/`publish` before `hello` are `hello_required`.
* A room is `(tenant_id, room)`: cross-tenant delivery is structurally
  impossible.
* The sender never receives its own `delivery`.
* Idle sessions (no traffic for 60 s) are reaped.
* Malformed frames, unknown types, or missing required fields are
  `bad_message`.

## Building and testing

```sh
gofmt -l .                  # must print nothing
go vet ./...                # must pass
go test -race ./...         # unit + e2e tests under the race detector
go build ./...              # compiles the binary + package
go run ./cmd/signal-server  # dev server on 0.0.0.0:8765
```

`VOXDESK_SIGNAL_PORT` overrides the listen port.

## Relation to the Rust counterpart

This service mirrors `voxdesk-signal` field-for-field on the wire. One
deliberate idiom difference, noted in code: the Rust token bucket
(`core/src/ratelimit.rs`) is a per-task `Copy` value, while the Go
`TokenBucket` is mutex-guarded so a single bucket can front a whole tenant.
The acquire/refill semantics are identical.
