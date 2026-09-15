package signal

import (
	"sync"
	"testing"
)

func TestTokenBucketStartsFullAndPermitsBurst(t *testing.T) {
	b := NewTokenBucket(5.0, 1.0, 0)
	for i := 0; i < 5; i++ {
		if !b.TryAcquire(0, 1.0) {
			t.Fatalf("acquire %d should succeed", i)
		}
	}
	if b.TryAcquire(0, 1.0) {
		t.Fatal("sixth acquire should fail")
	}
}

func TestTokenBucketRefillsAtDeclaredRate(t *testing.T) {
	b := NewTokenBucket(2.0, 2.0, 0)
	if !b.TryAcquire(0, 2.0) {
		t.Fatal("first acquire should succeed")
	}
	if b.TryAcquire(0, 1.0) {
		t.Fatal("acquire on empty bucket should fail")
	}
	// 1 second at 2/s refills 2 tokens.
	if !b.TryAcquire(1000, 2.0) {
		t.Fatal("acquire after refill should succeed")
	}
}

func TestTokenBucketNeverRefillsBeyondCapacity(t *testing.T) {
	b := NewTokenBucket(1.0, 100.0, 0)
	if !b.TryAcquire(0, 1.0) {
		t.Fatal("first acquire should succeed")
	}
	// Even after a long idle the bucket only holds `capacity` tokens.
	if got := b.Available(10_000); got != 1.0 {
		t.Fatalf("available = %v, want 1.0", got)
	}
}

func TestTokenBucketClockGoingBackwardsDoesNotRefill(t *testing.T) {
	b := NewTokenBucket(1.0, 1.0, 5_000)
	if !b.TryAcquire(5_000, 1.0) {
		t.Fatal("first acquire should succeed")
	}
	// now < last_refill: no refill, bucket stays empty.
	if b.TryAcquire(4_999, 1.0) {
		t.Fatal("backwards clock must not refill")
	}
}

func TestTokenBucketZeroCostAlwaysSucceeds(t *testing.T) {
	b := NewTokenBucket(0.0, 0.0, 0)
	if !b.TryAcquire(0, 0.0) {
		t.Fatal("zero-cost action must succeed")
	}
	if !b.TryAcquire(0, -1.0) {
		t.Fatal("negative-cost action must succeed")
	}
}

func TestTokenBucketConcurrentAcquireIsExact(t *testing.T) {
	// With a fixed clock and a mutex-protected bucket, exactly `capacity`
	// single-token acquisitions can ever succeed across goroutines.
	const capacity = 100.0
	b := NewTokenBucket(capacity, 1.0, 0)

	var wg sync.WaitGroup
	var successes int64
	var mu sync.Mutex
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			local := 0
			for i := 0; i < 1000; i++ {
				if b.TryAcquire(0, 1.0) {
					local++
				}
			}
			mu.Lock()
			successes += int64(local)
			mu.Unlock()
		}()
	}
	wg.Wait()
	if successes != int64(capacity) {
		t.Fatalf("total successes = %d, want %d", successes, int64(capacity))
	}
}
