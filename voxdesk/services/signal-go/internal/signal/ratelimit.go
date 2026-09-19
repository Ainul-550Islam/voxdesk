package signal

import "sync"

// TokenBucket is a token-bucket rate limiter with a logical clock, mirroring
// the Rust `ratelimit.rs` module exactly.
//
// The control plane enforces per-tenant rate limits on signaling traffic and
// on provider-facing actions (dial attempts, webhook deliveries). A token
// bucket is the right shape: it permits a burst up to the bucket's capacity
// and then a steady refill, and it degrades to "reject the excess" rather
// than "queue forever" — which is what a telephony control plane must do
// under load.
//
// Time is an explicit `nowMs` parameter, so the refill behaviour is fully
// deterministic and unit-testable without sleeping. One idiom difference
// from the Rust side: the Rust bucket is a per-task `Copy` value, whereas
// this Go bucket is shared behind a mutex so a single bucket can front a
// whole tenant's traffic. The acquire/refill semantics are identical.
type TokenBucket struct {
	mu              sync.Mutex
	capacity        float64
	refillPerSecond float64
	tokens          float64
	lastRefillMs    uint64
}

// NewTokenBucket returns a bucket that starts full. `capacity` is the burst
// size and `refillPerSecond` the sustained rate.
func NewTokenBucket(capacity, refillPerSecond float64, nowMs uint64) *TokenBucket {
	return &TokenBucket{
		capacity:        capacity,
		refillPerSecond: refillPerSecond,
		tokens:          capacity,
		lastRefillMs:    nowMs,
	}
}

func (b *TokenBucket) Capacity() float64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.capacity
}

func (b *TokenBucket) RefillPerSecond() float64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.refillPerSecond
}

func (b *TokenBucket) refill(nowMs uint64) {
	if nowMs <= b.lastRefillMs {
		return
	}
	elapsedSeconds := float64(nowMs-b.lastRefillMs) / 1000.0
	if b.tokens+elapsedSeconds*b.refillPerSecond > b.capacity {
		b.tokens = b.capacity
	} else {
		b.tokens += elapsedSeconds * b.refillPerSecond
	}
	b.lastRefillMs = nowMs
}

// Available returns the tokens currently available after refilling to nowMs.
func (b *TokenBucket) Available(nowMs uint64) float64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.refill(nowMs)
	return b.tokens
}

// TryAcquire attempts to spend n tokens. It never lets the bucket go
// negative. n <= 0 always succeeds (a zero-cost action cannot be limited).
func (b *TokenBucket) TryAcquire(nowMs uint64, n float64) bool {
	if n <= 0 {
		return true
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.refill(nowMs)
	if b.tokens >= n {
		b.tokens -= n
		return true
	}
	return false
}
