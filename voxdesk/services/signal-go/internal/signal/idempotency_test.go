package signal

import (
	"sync"
	"testing"
)

func TestIdempotencyFirstApplyWins(t *testing.T) {
	g := NewIdempotencyGuard()
	if !g.Apply("tenant-a", "event-1") {
		t.Fatal("first apply should win")
	}
	if g.Apply("tenant-a", "event-1") {
		t.Fatal("second apply should be a no-op")
	}
	if g.Len() != 1 {
		t.Fatalf("len = %d, want 1", g.Len())
	}
}

func TestIdempotencySameKeyDifferentTenantIsIndependent(t *testing.T) {
	g := NewIdempotencyGuard()
	if !g.Apply("tenant-a", "event-1") {
		t.Fatal("tenant-a apply should win")
	}
	if !g.Apply("tenant-b", "event-1") {
		t.Fatal("tenant-b apply should win")
	}
	if g.Len() != 2 {
		t.Fatalf("len = %d, want 2", g.Len())
	}
	if !g.Contains("tenant-a", "event-1") || !g.Contains("tenant-b", "event-1") {
		t.Fatal("contains should report both keys")
	}
	if g.Contains("tenant-c", "event-1") {
		t.Fatal("unknown key should not be present")
	}
}

func TestIdempotencyEmpty(t *testing.T) {
	g := NewIdempotencyGuard()
	if !g.IsEmpty() {
		t.Fatal("new guard should be empty")
	}
	g.Apply("t", "k")
	if g.IsEmpty() {
		t.Fatal("guard should not be empty after apply")
	}
}

func TestIdempotencyConcurrentSameKeyWinsExactlyOnce(t *testing.T) {
	g := NewIdempotencyGuard()
	const goroutines = 64
	var wg sync.WaitGroup
	var winners int64
	var mu sync.Mutex
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if g.Apply("tenant-a", "shared-key") {
				mu.Lock()
				winners++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if winners != 1 {
		t.Fatalf("winners = %d, want exactly 1", winners)
	}
	if g.Len() != 1 {
		t.Fatalf("len = %d, want 1", g.Len())
	}
}
