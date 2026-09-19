//! Exponential-backoff retry policy for webhook fan-out.
//!
//! The control plane fans webhooks out to every subscribed destination
//! (`broadcast.rs`) with per-destination idempotency keys (`idempotency.rs`).
//! This module supplies the third piece: the delay schedule for redelivery
//! after a failure. It is a pure function of the attempt number, so the
//! schedule itself is trivially testable and the transport layer can drive it
//! against a real clock later.

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RetryPolicy {
    /// Total tries including the first delivery (`max_attempts == 1` means no
    /// retry at all).
    pub max_attempts: u32,
    pub base_delay_ms: u64,
    pub factor: f64,
    pub max_delay_ms: u64,
}

impl RetryPolicy {
    /// The backoff delay to wait **before** the `attempt`-th overall try
    /// (0-based: attempt 0 is the first delivery, which has no delay).
    /// Returns `None` for the first try and once `attempt >= max_attempts`
    /// — the caller must give up rather than redeliver forever.
    pub fn delay_before_attempt(&self, attempt: u32) -> Option<u64> {
        if attempt == 0 {
            return None;
        }
        if attempt >= self.max_attempts {
            return None;
        }
        let factor = self.factor.powi((attempt - 1) as i32);
        let delay = self.base_delay_ms as f64 * factor;
        Some((delay.min(self.max_delay_ms as f64)) as u64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_try_has_no_delay_then_exponential_growth() {
        let p = RetryPolicy {
            max_attempts: 6,
            base_delay_ms: 100,
            factor: 2.0,
            max_delay_ms: 1_000,
        };
        assert_eq!(p.delay_before_attempt(0), None);
        assert_eq!(p.delay_before_attempt(1), Some(100));
        assert_eq!(p.delay_before_attempt(2), Some(200));
        assert_eq!(p.delay_before_attempt(3), Some(400));
        assert_eq!(p.delay_before_attempt(4), Some(800));
    }

    #[test]
    fn delay_is_capped() {
        let p = RetryPolicy {
            max_attempts: 7,
            base_delay_ms: 100,
            factor: 2.0,
            max_delay_ms: 1_000,
        };
        assert_eq!(p.delay_before_attempt(5), Some(1_000));
        assert_eq!(p.delay_before_attempt(6), Some(1_000));
    }

    #[test]
    fn gives_up_after_max_attempts() {
        let p = RetryPolicy {
            max_attempts: 3,
            base_delay_ms: 100,
            factor: 2.0,
            max_delay_ms: 1_000,
        };
        assert_eq!(p.delay_before_attempt(0), None);
        assert_eq!(p.delay_before_attempt(1), Some(100));
        assert_eq!(p.delay_before_attempt(2), Some(200));
        assert_eq!(p.delay_before_attempt(3), None);
    }

    #[test]
    fn no_retries_when_max_attempts_is_one() {
        let p = RetryPolicy {
            max_attempts: 1,
            base_delay_ms: 100,
            factor: 2.0,
            max_delay_ms: 1_000,
        };
        assert_eq!(p.delay_before_attempt(0), None);
        assert_eq!(p.delay_before_attempt(1), None);
    }
}
