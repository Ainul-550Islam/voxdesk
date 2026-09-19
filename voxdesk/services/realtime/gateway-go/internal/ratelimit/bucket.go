package ratelimit

import "time"

// Bucket is the classic lazy-refill token bucket, deliberately not
// thread-safe: it is the pure arithmetic core the Limiter wraps with a
// mutex. Lazy refill means no background goroutine and no timer — tokens
// are computed from elapsed wall-clock time at the moment of each check,
// so an idle connection costs exactly zero CPU and zero memory churn.
//
// Semantics, preserved verbatim from the gateway's original
// reader.go#allowMessage implementation:
//
//   - the bucket starts FULL (a fresh tab reconnecting after a drop must
//     be able to re-assert its subscriptions immediately);
//   - each allowance consumes exactly one whole token: when the banked
//     amount is in [0, 1) the frame is rejected and the fractional
//     remainder is kept (sub-token debt does not accumulate against the
//     client, it simply carries over);
//   - refill is capped at Burst, so quiet time banks at most one burst.
type Bucket struct {
	policy Policy
	tokens float64
	last   time.Time
}

// NewBucket returns a full bucket under the given policy, stamped at
// started. Callers pass time explicitly (rather than the bucket calling
// time.Now itself) so the Limiter can inject its clock for tests.
func NewBucket(policy Policy, started time.Time) *Bucket {
	return &Bucket{policy: policy, tokens: policy.Burst, last: started}
}

// AllowAt reports whether one unit of work may proceed at instant now,
// refilling lazily from the last checkpoint first. Exactly the gateway's
// original arithmetic:
//
//	tokens += elapsed * rate, capped at burst
//	if tokens < 1  → reject (keep the fractional remainder)
//	else           → tokens -= 1, allow
func (b *Bucket) AllowAt(now time.Time) bool {
	b.refill(now)
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// TokensAt exposes the banked amount at instant now WITHOUT consuming
// anything — diagnostics and tests. It applies the same lazy refill
// AllowAt would, so the bucket's checkpoint advances.
func (b *Bucket) TokensAt(now time.Time) float64 {
	b.refill(now)
	return b.tokens
}

// refill advances the bucket's checkpoint to now, adding elapsed*rate
// tokens capped at the policy's burst. A now BEFORE the last checkpoint
// (clock step-back, or a test driving time backwards) adds nothing and
// keeps the previous checkpoint — the budget a client already earned is
// never confiscated by time weirdness.
func (b *Bucket) refill(now time.Time) {
	if !now.After(b.last) {
		return
	}
	b.tokens += now.Sub(b.last).Seconds() * b.policy.RatePerSecond
	if b.tokens > b.policy.Burst {
		b.tokens = b.policy.Burst
	}
	b.last = now
}

// Policy returns the bucket's sizing.
func (b *Bucket) Policy() Policy { return b.policy }
