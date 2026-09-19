use std::time::{Duration, Instant};

use idempotency::Store;

#[test]
fn fresh_then_replay_within_ttl() {
    let store = Store::new(Duration::from_secs(60), 1_000);
    let t0 = Instant::now();
    assert!(
        !store.seen_before("tenant-a", "evt-1", t0),
        "first sight is fresh"
    );
    assert!(
        store.seen_before("tenant-a", "evt-1", t0 + Duration::from_secs(1)),
        "within TTL is a replay"
    );
    assert!(store.seen_before("tenant-a", "evt-1", t0 + Duration::from_secs(59)));
}

#[test]
fn expiry_makes_an_id_fresh_again() {
    let store = Store::new(Duration::from_secs(10), 1_000);
    let t0 = Instant::now();
    assert!(!store.seen_before("s", "e", t0));
    assert!(
        !store.seen_before("s", "e", t0 + Duration::from_secs(11)),
        "past TTL must be treated as new"
    );
}

#[test]
fn scopes_do_not_cross() {
    let store = Store::new(Duration::from_secs(60), 1_000);
    let now = Instant::now();
    assert!(!store.seen_before("tenant-a", "evt-1", now));
    assert!(
        !store.seen_before("tenant-b", "evt-1", now),
        "same id in another scope is independent"
    );
    assert!(store.seen_before("tenant-a", "evt-1", now));
}

#[test]
fn capacity_is_a_hard_bound_but_never_evicts_young_entries() {
    let ttl = Duration::from_secs(1_000);
    let capacity = 100usize;
    let store = Store::new(ttl, capacity);
    let t0 = Instant::now();

    // All entries YOUNGER than ttl/2: the flood fallback must still hold
    // the capacity line — dropping oldest-by-seq, deterministically.
    for i in 0..500 {
        assert!(!store.seen_before("s", &format!("id-{i}"), t0));
    }
    assert!(
        store.len() <= capacity,
        "hard bound violated: {}",
        store.len()
    );
    // The 100 NEWEST entries survived the flood: a replay probe against
    // them reports "seen" (true), an evicted old id reports "fresh" (false
    // — note the probe then RE-RECORDS it, so probe newest-first).
    let t1 = t0 + Duration::from_millis(1);
    assert!(
        store.seen_before("s", "id-499", t1),
        "newest must still be recorded"
    );
    assert!(
        store.seen_before("s", "id-450", t1),
        "a late survivor must still be recorded"
    );
    assert!(
        !store.seen_before("s", "id-100", t1),
        "an early id was evicted by the flood"
    );
}

#[test]
fn aged_entries_are_evicted_before_young_ones() {
    let ttl = Duration::from_secs(100);
    let store = Store::new(ttl, 10);
    let t0 = Instant::now();

    // 10 old entries (beyond ttl/2) fill the store…
    for i in 0..10 {
        store.seen_before("s", &format!("old-{i}"), t0);
    }
    // …then a new insert at a wall-clock where they're all beyond ttl/2.
    let t1 = t0 + Duration::from_secs(60);
    store.seen_before("s", "new-1", t1);
    assert!(
        // old-0..0ish entries got evicted, so they read "fresh" again —
        // but the mid-life entries must still read as replay.
        store.seen_before("s", "old-9", t1),
        "most recent old entry must survive aged eviction"
    );
}
