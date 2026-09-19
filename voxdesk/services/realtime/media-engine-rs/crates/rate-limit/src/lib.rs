//! rate-limit — the media plane's pacing primitives: token-bucket policy →
//! bucket arithmetic → a thread-safe limiter. Rust twin of gateway-go's
//! internal/ratelimit with identical semantics (lazy refill, integer token
//! spend, burst cap, full-at-birth), so an operator tuning one edge
//! relearns nothing on the other.
//!
//! Uses here: the per-session control-frame limiter (the control socket's
//! twin of the gateway's frame budget) and the per-SSRC RTP sanity ceiling
//! (a peer spraying 10× its negotiated rate is a broken sender, and its
//! excess must not consume forwarding CPU the honest peers paid for).

use std::sync::Mutex;
use std::time::Instant;

/// Sizing for one bucket: sustained refill rate and maximum bank.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Policy {
    pub rate_per_second: f64,
    pub burst: f64,
}

/// Construction rejects invalid policies LOUDLY (this is the Rust
/// equivalent of Go's NewPolicy error): a zero-rate limiter rejects
/// everything and a sub-unit burst rejects everything — both are the kind
/// of config arithmetic you want panic-at-boot, never silent-dead-traffic.
impl Policy {
    pub fn new(rate_per_second: f64, burst: f64) -> Result<Policy, PolicyError> {
        let p = Policy {
            rate_per_second,
            burst,
        };
        p.validate()?;
        Ok(p)
    }

    /// Compile-time-known-good construction (config already range-checked).
    /// Panics on an invalid policy: programmer error, not operator error.
    pub fn must(rate_per_second: f64, burst: f64) -> Policy {
        Policy::new(rate_per_second, burst).unwrap_or_else(|e| panic!("invalid static policy: {e}"))
    }

    /// Fail-closed fallback: 1/s with burst 1 — a limiter must never be
    /// constructible in a state that admits unbounded traffic.
    pub fn fallback() -> Policy {
        Policy {
            rate_per_second: 1.0,
            burst: 1.0,
        }
    }

    pub fn validate(&self) -> Result<(), PolicyError> {
        // partial_cmp (not !a>b) keeps the NaN branch honest: NaN maps to
        // None, which is a rejection here exactly like a non-positive rate.
        if !matches!(
            self.rate_per_second.partial_cmp(&0.0),
            Some(std::cmp::Ordering::Greater)
        ) {
            return Err(PolicyError::NonPositiveRate(self.rate_per_second));
        }
        if self.burst < 1.0 {
            return Err(PolicyError::SubUnitBurst(self.burst));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum PolicyError {
    NonPositiveRate(f64),
    SubUnitBurst(f64),
}

impl std::fmt::Display for PolicyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PolicyError::NonPositiveRate(r) => {
                write!(f, "rate per second must be positive, got {r}")
            }
            PolicyError::SubUnitBurst(b) => write!(
                f,
                "burst must be at least 1 (below that every unit is rejected), got {b}"
            ),
        }
    }
}

impl std::error::Error for PolicyError {}

/// The pure arithmetic: lazy refill, spend exactly one whole token per
/// allowance, clock-step-back never confiscates banked budget. NOT
/// thread-safe by itself (Limiter wraps it); kept `pub` for tests and the
/// jitter-buffer's internal ceilings that already hold an outer lock.
pub struct Bucket {
    policy: Policy,
    tokens: f64,
    last: Instant,
}

impl Bucket {
    pub fn full_at(policy: Policy, started: Instant) -> Bucket {
        Bucket {
            policy,
            tokens: policy.burst,
            last: started,
        }
    }

    /// One unit of work at `now`. Fractional remainders carry; refill caps
    /// at burst; a backwards clock adds nothing and moves no checkpoint.
    pub fn allow_at(&mut self, now: Instant) -> bool {
        self.refill(now);
        if self.tokens < 1.0 {
            return false;
        }
        self.tokens -= 1.0;
        true
    }

    pub fn tokens_at(&mut self, now: Instant) -> f64 {
        self.refill(now);
        self.tokens
    }

    fn refill(&mut self, now: Instant) {
        if now <= self.last {
            return;
        }
        self.tokens += now.duration_since(self.last).as_secs_f64() * self.policy.rate_per_second;
        if self.tokens > self.policy.burst {
            self.tokens = self.policy.burst;
        }
        self.last = now;
    }

    pub fn policy(&self) -> Policy {
        self.policy
    }
}

/// The thread-safe facade every traffic source uses. `allow()` is one
/// short mutex acquisition with no allocation and no syscalls — on the
/// control path that is free; on the RTP path it is per-SSRC (fast enough:
/// benchmarked in ./benches) and contended only across legs of the SAME
/// source.
pub struct Limiter {
    inner: Mutex<Bucket>,
}

impl Limiter {
    pub fn new(policy: Policy) -> Limiter {
        let policy = if policy.validate().is_ok() {
            policy
        } else {
            Policy::fallback()
        };
        Limiter {
            inner: Mutex::new(Bucket::full_at(policy, Instant::now())),
        }
    }

    pub fn allow(&self) -> bool {
        let mut b = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        b.allow_at(Instant::now())
    }

    pub fn tokens(&self) -> f64 {
        let mut b = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        b.tokens_at(Instant::now())
    }

    pub fn policy(&self) -> Policy {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .policy()
    }
}
