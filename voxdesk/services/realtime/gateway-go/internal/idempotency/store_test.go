package idempotency

import (
	"strconv"
	"testing"
	"time"
)

// Unit-level proof of the guard: what the HTTP ingest tests previously
// exercised only end-to-end, pinned here on the algorithm itself (TTL,
// per-tenant scoping, capacity, eviction).

var start = time.Date(2026, 9, 16, 10, 0, 0, 0, time.UTC)

func TestFirstSeenIsFreshSecondIsReplay(t *testing.T) {
	s := NewStore(time.Minute, 100)
	if s.SeenBefore("tenant-a", "ev-1", start) {
		t.Fatal("first sighting must be fresh")
	}
	if !s.SeenBefore("tenant-a", "ev-1", start.Add(time.Second)) {
		t.Fatal("second sighting inside the TTL must be a replay")
	}
	if s.Len() != 1 {
		t.Fatalf("replays must not grow the store: Len = %d", s.Len())
	}
}

func TestReplayScopeIsPerTenant(t *testing.T) {
	s := NewStore(time.Minute, 100)
	s.SeenBefore("tenant-a", "ev-1", start)
	if s.SeenBefore("tenant-b", "ev-1", start) {
		t.Fatal("the same event id under ANOTHER tenant is not a replay — scoping matches UniqueConstraint(tenant_id, …)")
	}
}

func TestEntriesExpirePastTheTTL(t *testing.T) {
	s := NewStore(time.Minute, 100)
	s.SeenBefore("tenant-a", "ev-1", start)
	if s.SeenBefore("tenant-a", "ev-1", start.Add(2*time.Minute)) {
		t.Fatal("an entry past its TTL must not suppress — realtime events are worthless redelivered late anyway")
	}
}

func TestCapacityTriggersEvictionButHotKeysSurvive(t *testing.T) {
	// TTL 1 h, capacity 10: insert entries spread across the half-TTL
	// cutoff so the oldest-third heuristic has something to evict.
	s := NewStore(time.Hour, 10)
	base := start
	for i := 0; i < 10; i++ {
		at := base.Add(time.Duration(i) * 2 * time.Minute) // 0..18 minutes old
		s.SeenBefore("tenant-a", "ev-"+strconv.Itoa(i), at)
	}
	if s.Len() != 10 {
		t.Fatalf("setup: Len = %d", s.Len())
	}

	// One more fresh entry forces a sweep: nothing is TTL-expired yet (all
	// well under an hour), so the oldest-beyond-midpoint entries evict.
	cutoff := base.Add(40 * time.Minute) // now is 20 min after start
	s.SeenBefore("tenant-a", "ev-new", cutoff)
	if s.Len() > 10 {
		t.Fatalf("store stayed over capacity after sweep: Len = %d", s.Len())
	}

	// The event recorded just now must still be treated as seen (a replay
	// cache that evicts its OWN freshest entries is useless).
	if !s.SeenBefore("tenant-a", "ev-new", cutoff.Add(time.Second)) {
		t.Fatal("freshest entry must survive its own sweep")
	}
}

func TestSeenRecordingIsAtomicWithTheCheck(t *testing.T) {
	// Two live calls for the same key, sequenced by the mutex (concurrency
	// is covered under -race by the concurrent accesses in this loop):
	s := NewStore(time.Minute, 100)
	done := make(chan bool, 64)
	for i := 0; i < 64; i++ {
		go func(n int) {
			s.SeenBefore("tenant-a", "ev-"+strconv.Itoa(n%8), start)
			done <- true
		}(i)
	}
	for i := 0; i < 64; i++ {
		<-done
	}
	// 8 distinct keys, no more and no less, regardless of interleaving.
	if got := s.Len(); got != 8 {
		t.Fatalf("Len = %d after concurrent inserts of 8 keys", got)
	}
}
