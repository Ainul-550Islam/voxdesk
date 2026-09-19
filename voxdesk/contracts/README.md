# Wire contracts (Phase 0)

The cross-service wire contract for the polyglot roadmap. Protobuf is the
single source of truth; every non-Python service (Rust control plane, C++
media plane, Go ops tooling) generates its types from these files, so two
services can never disagree about a message shape.

## Layout

```
contracts/proto/voxdesk/contracts/v1/
├── common.proto    shared primitives: Uuid, TenantContext, Idempotency, ErrorInfo, Speaker
├── session.proto   call/session lifecycle + transfer state machine (SessionControl RPC)
├── media.proto     media-plane admit/leave + transcription taps (MediaControl RPC)
├── usage.proto     usage metering events (UsageMetric / UsageEventType / UsageRecorded)
└── webhook.proto   outbound integration delivery (CrmEventType / WebhookDelivery)
```

Package: `voxdesk.contracts.v1`. Generated Go module path is declared on every
file (`go_package`) so the first code-generation run is deterministic.

## Rules every message must obey

These are checked by `scripts/verify_contracts.py` (CI + `make contracts-check`)
and by `tests/test_contracts.py` (ordinary backend suite):

1. **Tenant scoping.** Every operational message carries a `TenantContext`
   (or, at minimum, a `tenant_id`) field. Only the four shared primitives
   (`Uuid`, `TenantContext`, `Idempotency`, `ErrorInfo`) are exempt. A message
   that can cross a service boundary without a tenant binding is rejected.
2. **Idempotency.** Every *event* embeds `Idempotency`. A receiver that has
   already applied `(tenant_id, idempotency_key)` treats a redelivery as a
   no-op — the same exactly-once contract the database enforces via
   `UniqueConstraint(tenant_id, idempotency_key)`.
3. **Enum vocabulary = database vocabulary.** Enum member names must match the
   Python enum member names in `app/db/models.py` exactly, because SQLAlchemy
   persists the member *name* (not the value) into PostgreSQL. `CrmEventType`
   additionally maps each member to its dotted published value
   (`CALL_COMPLETED` → `call.completed`).

## Adding or changing a message

1. Edit the `.proto` file (never edit generated code — regenerate it).
2. Run `python scripts/verify_contracts.py` (needs `protoc`; install
   `protobuf-compiler` or pass `--protoc /path/to/protoc`).
3. Run `pytest tests/test_contracts.py` for the protoc-free textual checks.
4. If you add a shared primitive, add it to `PRIMITIVE_MESSAGES` in
   `scripts/verify_contracts.py` — the test suite will remind you.

## Regenerating bindings

No bindings are generated yet (they arrive with the first consumer of each
language). When they are, generated output goes under `contracts/gen/` and is
excluded from first-party line counts and from the repository unless a
consumer needs it vendored.
