package ratelimit

import (
	"sync"
	"time"
)

// Limiter is the concurrency-safe facade every traffic source uses. It
// owns a Bucket plus the clock, and serializes access internally — the
// original gateway implementation held the bucket inside the Connection's
// own mutex; the extraction lifts that guard INTO the type so a future
// caller cannot forget it.
type Limiter struct {
	mu     sync.Mutex
	bucket *Bucket
	// now is replaceable in tests (WithClock) so refill arithmetic can be
	// driven without sleeping real time.
	now func() time.Time
}

// Option customizes a Limiter at construction.
type Option func(*Limiter)

// WithClock replaces the wall clock (tests only).
func WithClock(now func() time.Time) Option {
	return func(l *Limiter) { l.now = now }
}

// New returns a Limiter under the given policy, its bucket starting full.
// An invalid policy (rate <= 0, burst < 1) falls back to DefaultPolicy —
// a limiter must NEVER be constructable in a state that silently admits
// unbounded traffic; the strict fallback fails closed instead.
func New(policy Policy, opts ...Option) *Limiter {
	if policy.Validate() != nil {
		policy = DefaultPolicy()
	}
	l := &Limiter{now: time.Now}
	for _, opt := range opts {
		opt(l)
	}
	l.bucket = NewBucket(policy, l.now())
	return l
}

// Allow reports whether one unit of inbound work may proceed. This is the
// whole contract the read loop relies on.
func (l *Limiter) Allow() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.bucket.AllowAt(l.now())
}

// Tokens reports the currently banked amount (diagnostics/tests only).
func (l *Limiter) Tokens() float64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.bucket.TokensAt(l.now())
}

// Policy returns the limiter's effective policy (post-fallback).
func (l *Limiter) Policy() Policy {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.bucket.Policy()
}
