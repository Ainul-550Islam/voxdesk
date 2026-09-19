//! Concurrency: check-and-record must be exactly-once under contention.

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use idempotency::Store;

#[test]
fn concurrent_first_sight_admits_exactly_one() {
    let store = Arc::new(Store::new(Duration::from_secs(60), 10_000));
    let now = Instant::now();

    let mut handles = Vec::new();
    for _ in 0..8 {
        let store = Arc::clone(&store);
        handles.push(thread::spawn(move || {
            let mut fresh = 0usize;
            for i in 0..200 {
                // All 8 threads race the SAME 200 ids.
                if !store.seen_before("s", &format!("k-{i}"), now) {
                    fresh += 1;
                }
            }
            fresh
        }));
    }
    let admitted: usize = handles.into_iter().map(|h| h.join().unwrap()).sum();
    assert_eq!(
        admitted, 200,
        "across all threads, each id may be 'fresh' exactly once: got {admitted}"
    );
}
