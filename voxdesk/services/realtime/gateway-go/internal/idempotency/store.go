// Package idempotency owns the ingest replay guard: which (tenant, event)
// pairs this edge has already fanned out, remembered long enough to make
// publisher retries converge, and NO longer.
//
// It mirrors the Python side's MessageWebhookReceipt / BillingWebhookReceipt
// split in spirit: database-durable suppression lives THERE (the API's own
// exactly-once machinery); this hot cache is the cheap first wall for a
// stateless edge that has no database. Losing an entry costs a duplicate
// DELIVERY to browsers — annoying, never state-corrupting — which is the
// entire reason an evicting cache is acceptable here while it would not be
// for billing or webhooks.
package idempotency

import (
	"sync"
	"time"
)

// Store is the replay cache. All methods are safe for concurrent use; a
// single mutex is correct at this cardinality (sweeps are amortized, and
// the map is small by construction).
type Store struct {
	mu        sync.Mutex
	seen      map[string]time.Time
	ttl       time.Duration
	capacity  int
	lastSweep time.Time
}

// NewStore builds the guard: entries age out after ttl, and the map is
// bounded at capacity entries (see sweep for the eviction policy when
// pressure exceeds that anyway).
func NewStore(ttl time.Duration, capacity int) *Store {
	return &Store{
		seen:     make(map[string]time.Time),
		ttl:      ttl,
		capacity: capacity,
	}
}

// key joins tenant and event id: replay scope is per-tenant, matching every
// other idempotency constraint in the system (UniqueConstraint(tenant_id, …)).
func key(tenantID, eventID string) string { return tenantID + "|" + eventID }

// SeenBefore reports whether this (tenant, event) was already fanned out;
// when it was not, it is recorded. Sweeping is amortized: at most once per
// TTL window per capacity pressure, never per request on the hot path.
func (s *Store) SeenBefore(tenantID, eventID string, now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := key(tenantID, eventID)
	if at, ok := s.seen[k]; ok && now.Sub(at) < s.ttl {
		return true
	}
	if len(s.seen) >= s.capacity || now.Sub(s.lastSweep) >= s.ttl {
		s.sweep(now)
	}
	s.seen[k] = now
	return false
}

// Len reports live entries — the gauge substrate for observability and the
// capacity assertions in tests.
func (s *Store) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.seen)
}

// sweep drops expired entries; if the map is STILL over capacity afterwards
// (a genuinely high-cardinality burst), the oldest ~10% are evicted. Losing
// an old replay record only ever causes a duplicate DELIVERY — which is why
// this eviction exists here and must never exist for webhooks or billing.
//
// Caller must hold s.mu.
func (s *Store) sweep(now time.Time) {
	s.lastSweep = now
	for k, at := range s.seen {
		if now.Sub(at) >= s.ttl {
			delete(s.seen, k)
		}
	}
	if len(s.seen) < s.capacity {
		return
	}
	// Oldest-first eviction of 10% without a full sort: any entry older
	// than the TTL's midpoint goes first, then give up — the map self-heals
	// on the next sweep.
	cutoff := now.Add(-s.ttl / 2)
	evicted := 0
	target := s.capacity / 10
	for k, at := range s.seen {
		if at.Before(cutoff) {
			delete(s.seen, k)
			evicted++
			if evicted >= target {
				break
			}
		}
	}
}
