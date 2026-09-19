use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use rate_limit::{Bucket, Limiter, Policy};

#[test]
fn policy_validation() {
    assert!(Policy::new(20.0, 40.0).is_ok());
    assert!(Policy::new(0.0, 40.0).is_err());
    assert!(Policy::new(-3.0, 40.0).is_err());
    assert!(Policy::new(20.0, 0.5).is_err());
    assert_eq!(
        Policy::must(2.0, 4.0),
        Policy {
            rate_per_second: 2.0,
            burst: 4.0
        }
    );
}

#[test]
#[should_panic]
fn must_panics_on_bad_policy() {
    let _ = Policy::must(0.0, 5.0);
}

#[test]
fn bucket_starts_full_and_depletes() {
    let t0 = Instant::now();
    let mut b = Bucket::full_at(Policy::must(10.0, 5.0), t0);
    for i in 0..5 {
        assert!(
            b.allow_at(t0),
            "burst frame {i} rejected from a full bucket"
        );
    }
    assert!(!b.allow_at(t0), "sixth unit must reject");
    // 50ms at 10/s = 0.5 token: not enough for a whole unit.
    assert!(!b.allow_at(t0 + Duration::from_millis(50)));
    // Fractional remainder carries to the next 50ms.
    assert!(b.allow_at(t0 + Duration::from_millis(100)));
    assert!(!b.allow_at(t0 + Duration::from_millis(100)));
}

#[test]
fn refill_caps_at_burst_and_backwards_clock_is_neutral() {
    let t0 = Instant::now();
    let mut b = Bucket::full_at(Policy::must(100.0, 3.0), t0);
    // An "hour" passes: uncapped that would be 360k tokens.
    let t1 = t0 + Duration::from_secs(3600);
    assert_eq!(b.tokens_at(t1), 3.0, "quiet time banks at most one burst");
    // Step the clock backwards: nothing refills (the burst spends down
    // without replenishment) and nothing is confiscated either.
    for i in 0..3 {
        assert!(b.allow_at(t0), "banked token {i} must survive a step-back");
    }
    assert!(!b.allow_at(t0), "no refill happened from a backwards clock");
}

#[test]
fn invalid_limiter_falls_back_closed() {
    let l = Limiter::new(Policy {
        rate_per_second: 0.0,
        burst: 0.0,
    });
    assert_eq!(l.policy(), Policy::fallback());
    assert!(l.allow(), "fallback admits its single token");
    assert!(!l.allow(), "and then it stops");
}

#[test]
fn concurrent_accounting_is_exact() {
    let l = Arc::new(Limiter::new(Policy::must(0.0001, 64.0))); // ~no refill during test
    let admitted = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let mut handles = Vec::new();
    for _ in 0..8 {
        let l = Arc::clone(&l);
        let admitted = Arc::clone(&admitted);
        handles.push(thread::spawn(move || {
            for _ in 0..64 {
                if l.allow() {
                    admitted.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                }
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    assert_eq!(
        admitted.load(std::sync::atomic::Ordering::Relaxed),
        64,
        "concurrent spend must drain exactly the burst"
    );
}
