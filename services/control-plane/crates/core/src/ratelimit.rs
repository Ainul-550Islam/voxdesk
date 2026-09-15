//! Token-bucket rate limiter with a logical clock.
//!
//! The control plane enforces per-tenant rate limits on signaling traffic and
//! on provider-facing actions (dial attempts, webhook deliveries). A token
//! bucket is the right shape: it permits a burst up to the bucket's capacity
//! and then a steady refill, and it degrades to "reject the excess" rather
//! than "queue forever" — which is what a telephony control plane must do
//! under load.
//!
//! Time is an explicit `now_ms` parameter, so the refill behaviour is fully
//! deterministic and unit-testable without sleeping.

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TokenBucket {
    capacity: f64,
    refill_per_second: f64,
    tokens: f64,
    last_refill_ms: u64,
}

impl TokenBucket {
    /// A bucket that starts full. `capacity` is the burst size and
    /// `refill_per_second` the sustained rate.
    pub fn new(capacity: f64, refill_per_second: f64, now_ms: u64) -> Self {
        TokenBucket {
            capacity,
            refill_per_second,
            tokens: capacity,
            last_refill_ms: now_ms,
        }
    }

    pub fn capacity(&self) -> f64 {
        self.capacity
    }

    pub fn refill_per_second(&self) -> f64 {
        self.refill_per_second
    }

    fn refill(&mut self, now_ms: u64) {
        if now_ms <= self.last_refill_ms {
            return;
        }
        let elapsed_seconds = (now_ms - self.last_refill_ms) as f64 / 1000.0;
        self.tokens = (self.tokens + elapsed_seconds * self.refill_per_second).min(self.capacity);
        self.last_refill_ms = now_ms;
    }

    /// Tokens currently available after refilling to `now_ms`.
    pub fn available(&mut self, now_ms: u64) -> f64 {
        self.refill(now_ms);
        self.tokens
    }

    /// Attempts to spend `n` tokens. Never allows the bucket to go negative.
    /// `n <= 0` always succeeds (a zero-cost action cannot be rate limited).
    pub fn try_acquire(&mut self, now_ms: u64, n: f64) -> bool {
        if n <= 0.0 {
            return true;
        }
        self.refill(now_ms);
        if self.tokens >= n {
            self.tokens -= n;
            true
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn starts_full_and_permits_a_burst() {
        let mut b = TokenBucket::new(5.0, 1.0, 0);
        for _ in 0..5 {
            assert!(b.try_acquire(0, 1.0));
        }
        assert!(!b.try_acquire(0, 1.0));
    }

    #[test]
    fn refills_at_the_declared_rate() {
        let mut b = TokenBucket::new(2.0, 2.0, 0);
        assert!(b.try_acquire(0, 2.0));
        assert!(!b.try_acquire(0, 1.0));
        // 1 second at 2/s refills 2 tokens.
        assert!(b.try_acquire(1000, 2.0));
    }

    #[test]
    fn never_refills_beyond_capacity() {
        let mut b = TokenBucket::new(1.0, 100.0, 0);
        assert!(b.try_acquire(0, 1.0));
        // Even after a long idle the bucket only holds `capacity` tokens.
        assert_eq!(b.available(10_000), 1.0);
    }

    #[test]
    fn clock_going_backwards_does_not_refill() {
        let mut b = TokenBucket::new(1.0, 1.0, 5_000);
        assert!(b.try_acquire(5_000, 1.0));
        // now < last_refill: no refill, bucket stays empty.
        assert!(!b.try_acquire(4_999, 1.0));
    }

    #[test]
    fn zero_cost_action_always_succeeds() {
        let mut b = TokenBucket::new(0.0, 0.0, 0);
        assert!(b.try_acquire(0, 0.0));
    }
}
