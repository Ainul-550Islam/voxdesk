// Package ratelimit owns the gateway's inbound frame pacing: the token
// bucket primitive, the policy that sizes it, and the concurrency-safe
// limiter every WebSocket connection carries.
//
// It exists for one reason (unchanged from when this logic lived inside
// internal/websocket's reader): a hijacked or buggy browser tab must not be
// able to keep the gateway CPU-busy decoding frames unboundedly. The
// extraction into a package makes the primitive unit-testable on its own
// clock and reusable for any future inbound surface (e.g. a second edge).
package ratelimit

import "fmt"

// Policy sizes a token bucket: sustained replenish rate and the maximum
// burst a client may bank while quiet.
//
// The gateway's production values (config.DefaultMessageRatePerSecond /
// DefaultMessageBurst, 20/s with a bank of 40) are chosen for dashboards:
// a human clicking produce a handful of frames a second, a reconnecting
// tab re-asserts its subscriptions in one burst, and anything sustained
// above 20 fps is a bug or an attack, not a workload.
type Policy struct {
	// RatePerSecond is the sustained refill rate in tokens (frames) per
	// second. Must be > 0.
	RatePerSecond float64
	// Burst is the bucket's capacity — the most tokens a client can bank
	// while idle and therefore the largest instantaneous burst allowed.
	// Must be >= 1 (below 1 every frame would be rejected forever).
	Burst float64
}

// NewPolicy validates and normalizes a policy. It returns an error rather
// than silently clamping: a zero-rate limiter rejects EVERYTHING, which is
// exactly the misconfiguration that should be loud at construction time,
// not discovered as "no client can ever send" in production.
func NewPolicy(ratePerSecond, burst float64) (Policy, error) {
	p := Policy{RatePerSecond: ratePerSecond, Burst: burst}
	return p, p.Validate()
}

// MustPolicy is NewPolicy for call sites that pass compile-time-known-good
// configuration (the gateway's config has already range-checked these
// values at boot); a bad policy here is a programming error, so it panics.
func MustPolicy(ratePerSecond, burst float64) Policy {
	p, err := NewPolicy(ratePerSecond, burst)
	if err != nil {
		panic(fmt.Sprintf("ratelimit: invalid static policy: %v", err))
	}
	return p
}

// DefaultPolicy returns the package's own safe fallback (1 frame/s, burst
// 1) — deliberately stricter than the gateway's production defaults, so a
// caller that forgets to wire config errs on the side of refusing traffic
// rather than of permitting floods.
func DefaultPolicy() Policy {
	return Policy{RatePerSecond: 1, Burst: 1}
}

// Validate reports why a policy is unusable, or nil when it is sound.
func (p Policy) Validate() error {
	switch {
	case p.RatePerSecond <= 0:
		return fmt.Errorf("rate per second must be positive, got %v", p.RatePerSecond)
	case p.Burst < 1:
		return fmt.Errorf("burst must be at least 1 (below that every frame is rejected), got %v", p.Burst)
	}
	return nil
}

// String renders the policy for logs: "20/s (burst 40)".
func (p Policy) String() string {
	return fmt.Sprintf("%g/s (burst %g)", p.RatePerSecond, p.Burst)
}
