package ratelimit

import (
	"sync"
	"testing"
	"time"
)

func staticClock(start time.Time) (func() time.Time, *time.Time) {
	now := start
	ptr := &now
	return func() time.Time { return *ptr }, ptr
}

func TestPolicyValidation(t *testing.T) {
	t.Parallel()
	if p, err := NewPolicy(20, 40); err != nil || p.RatePerSecond != 20 || p.Burst != 40 {
		t.Fatalf("valid policy rejected: %v %v", p, err)
	}
	if _, err := NewPolicy(0, 40); err == nil {
		t.Fatal("zero rate must be rejected (it would dead-letter every frame)")
	}
	if _, err := NewPolicy(20, 0.5); err == nil {
		t.Fatal("sub-unit burst must be rejected (it would reject every frame forever)")
	}
	if got := DefaultPolicy(); got.Validate() != nil {
		t.Fatalf("default policy must itself be valid: %v", got)
	}
}

func TestLimiterStartsFullAndDepletesBurst(t *testing.T) {
	t.Parallel()
	clock, now := staticClock(time.Unix(1_700_000_000, 0))
	l := New(MustPolicy(10, 5), WithClock(clock))

	for i := 0; i < 5; i++ {
		if !l.Allow() {
			t.Fatalf("burst frame %d rejected from a full bucket", i)
		}
	}
	if l.Allow() {
		t.Fatal("sixth frame must be rejected once the burst is spent")
	}
	// Fractional time advances refill fractionally: 50 ms at 10/s = 0.5
	// token, not enough for a whole frame on an empty bucket.
	*now = now.Add(50 * time.Millisecond)
	if l.Allow() {
		t.Fatal("0.5 token must not admit a whole frame")
	}
	// Another 50 ms completes the token (fractional remainder carries).
	*now = now.Add(50 * time.Millisecond)
	if !l.Allow() {
		t.Fatal("accumulated full token must admit exactly one frame")
	}
	if l.Allow() {
		t.Fatal("the burst must not magically refill")
	}
}

func TestLimiterRefillCappedAtBurst(t *testing.T) {
	t.Parallel()
	clock, now := staticClock(time.Unix(1_700_000_000, 0))
	l := New(MustPolicy(100, 3), WithClock(clock))

	// Sleep "for an hour": at 100/s that would be 360k tokens uncapped.
	*now = now.Add(time.Hour)
	for i := 0; i < 3; i++ {
		if !l.Allow() {
			t.Fatalf("banked burst frame %d rejected", i)
		}
	}
	if l.Allow() {
		t.Fatal("refill must cap at burst; an idle client banks at most one burst")
	}
}

func TestLimiterClockStepBackCannotConfiscate(t *testing.T) {
	t.Parallel()
	clock, now := staticClock(time.Unix(1_700_000_000, 0))
	l := New(MustPolicy(10, 5), WithClock(clock))

	l.Allow() // spend one
	*now = now.Add(-time.Minute)
	if got := l.Tokens(); got != 4 {
		t.Fatalf("backwards clock must not change the bank, got %v tokens", got)
	}
	if !l.Allow() {
		t.Fatal("already-earned budget must survive a clock step-back")
	}
}

func TestLimiterInvalidPolicyFailsClosed(t *testing.T) {
	t.Parallel()
	l := New(Policy{RatePerSecond: 0, Burst: 0}) // never valid
	if got := l.Policy(); got != DefaultPolicy() {
		t.Fatalf("invalid policy must fall back to the strict default, got %v", got)
	}
	if !l.Allow() || l.Allow() {
		t.Fatal("default policy admits exactly its burst (1), then stops")
	}
}

func TestLimiterConcurrentAllowIsSafeAndAccountingHolds(t *testing.T) {
	t.Parallel()
	clock, _ := staticClock(time.Unix(1_700_000_000, 0))
	const burst = 64
	l := New(MustPolicy(1, burst), WithClock(clock)) // ~no refill during test

	var wg sync.WaitGroup
	allowed := make(chan struct{}, 4*burst)
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < burst; i++ {
				if l.Allow() {
					allowed <- struct{}{}
				}
			}
		}()
	}
	wg.Wait()
	close(allowed)
	if n := len(allowed); n != burst {
		t.Fatalf("concurrent spend must be exactly consistent: %d admitted, want %d", n, burst)
	}
}
