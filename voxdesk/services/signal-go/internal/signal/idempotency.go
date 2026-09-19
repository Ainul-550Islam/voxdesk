package signal

import "sync"

// IdempotencyGuard is an exactly-once guard for cross-service events,
// mirroring the Rust `idempotency.rs` module exactly.
//
// It mirrors the database-level UniqueConstraint(tenant_id, idempotency_key)
// guarantee (see `alembic/versions/0011_side_effect_exactly_once.py` and the
// CRM / billing / appointment idempotency columns): a redelivery of an event
// already applied for (tenant, key) is a no-op.
type IdempotencyGuard struct {
	mu   sync.Mutex
	seen map[idempotencyKey]struct{}
}

type idempotencyKey struct {
	tenant string
	key    string
}

func NewIdempotencyGuard() *IdempotencyGuard {
	return &IdempotencyGuard{seen: make(map[idempotencyKey]struct{})}
}

// Apply returns true the first time (tenant, key) is applied, false on every
// subsequent application (the caller must treat false as "already done; do
// not perform the side effect again").
func (g *IdempotencyGuard) Apply(tenant, key string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	k := idempotencyKey{tenant: tenant, key: key}
	if _, ok := g.seen[k]; ok {
		return false
	}
	g.seen[k] = struct{}{}
	return true
}

// Contains reports whether (tenant, key) has already been applied.
func (g *IdempotencyGuard) Contains(tenant, key string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	_, ok := g.seen[idempotencyKey{tenant: tenant, key: key}]
	return ok
}

// Len reports how many distinct (tenant, key) pairs have been applied.
func (g *IdempotencyGuard) Len() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return len(g.seen)
}

// IsEmpty reports whether nothing has been applied yet.
func (g *IdempotencyGuard) IsEmpty() bool {
	return g.Len() == 0
}
