//! idempotency — exactly-once bookkeeping for retried operations, ported
//! from gateway-go's internal/idempotency with the same contract:
//!
//! * bounded memory (capacity + TTL, never an unbounded map);
//! * atomic check-and-record (one call — a check-then-insert race would
//!   let two retries both look "fresh");
//! * tenant-scoped keys (a replayed event id must never suppress a
//!   DIFFERENT tenant's same-numbered event);
//! * lazy sweeping amortized over inserts plus oldest-beyond-midpoint
//!   eviction at capacity, so worst-case memory is the cap, always.
//!
//! Media-plane uses: retransmitted signaling frames (publish answers),
//! LiveKit webhook redeliveries, and re-sent RECORD start/stop requests.
//! Media DATAGRAMS deliberately do NOT use this store — RTP has its own
//! sequence arithmetic (see streams/replay.rs), which is cheaper and
//! correct at line rate.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// One recorded operation.
struct Entry {
    seen_at: Instant,
    seq: u64, // insertion order for oldest-first eviction under TTL pressure
}

/// The bounded replay store.
pub struct Store {
    inner: Mutex<Inner>,
    ttl: Duration,
    capacity: usize,
}

struct Inner {
    map: HashMap<(String, String), Entry>, // (scope, id) → entry
    clock: u64,                            // monotonically increasing seq source
    since_sweep: usize,                    // inserts since last lazy sweep
}

/// How often (in inserts) a full TTL sweep happens. 1k inserts between
/// sweeps keeps a saturated 50k-entry store's sweep cost sub-millisecond
/// amortized, same trade the Go side banks.
const SWEEP_EVERY: usize = 1_024;

impl Store {
    /// ttl: how long an id stays "seen". capacity: hard memory bound; at
    /// capacity the oldest ~10% beyond a mid-life cutoff are evicted (a
    /// flood of NEW ids must not evict everything recent, and a flood of
    /// old ids must not evict anything it added a moment ago).
    pub fn new(ttl: Duration, capacity: usize) -> Store {
        Store {
            inner: Mutex::new(Inner {
                map: HashMap::new(),
                clock: 0,
                since_sweep: 0,
            }),
            ttl,
            capacity: capacity.max(64),
        }
    }

    /// Atomically checks and records (scope, id). Returns true when the id
    /// was ALREADY recorded and is still within its TTL — the caller then
    /// suppresses the retry (the 200-duplicate path on ingest; the
    /// once-only answer on a retransmitted signaling frame).
    pub fn seen_before(&self, scope: &str, id: &str, now: Instant) -> bool {
        let mut inner = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        let key = (scope.to_string(), id.to_string());

        if let Some(entry) = inner.map.get(&key) {
            if now.duration_since(entry.seen_at) <= self.ttl {
                return true;
            }
            // Expired entry: it loses to the fresh retry below.
        }

        inner.clock += 1;
        let seq = inner.clock;
        inner.map.insert(key, Entry { seen_at: now, seq });

        // Amortized housekeeping: full TTL sweep every SWEEP_EVERY inserts,
        // capacity eviction immediately when the hard bound is crossed.
        inner.since_sweep += 1;
        if inner.since_sweep >= SWEEP_EVERY {
            inner.since_sweep = 0;
            let ttl = self.ttl;
            inner
                .map
                .retain(|_, e| now.duration_since(e.seen_at) <= ttl);
        }
        if inner.map.len() > self.capacity {
            evict_oldest(&mut inner, self.capacity, self.ttl, now);
        }
        false
    }

    /// Current entry count (diagnostics/tests).
    pub fn len(&self) -> usize {
        self.inner
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .map
            .len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Eviction under capacity pressure: drop the oldest entries, but never
/// more than down to 90% of capacity, and only entries older than HALF the
/// TTL — exactly the Go store's rule, which exists so a legitimately
/// retried id can never be evicted "young" (TTL/2 is still generous against
/// provider retry schedules) and a burst at capacity costs one partial
/// compaction, not a flag day.
fn evict_oldest(inner: &mut Inner, capacity: usize, ttl: Duration, now: Instant) {
    let cutoff = ttl / 2;
    let target = capacity * 9 / 10;

    // Collect (seq, key) of eviction candidates older than the cutoff.
    let mut aged: Vec<(u64, (String, String))> = inner
        .map
        .iter()
        .filter(|(_, e)| now.duration_since(e.seen_at) > cutoff)
        .map(|(k, e)| (e.seq, k.clone()))
        .collect();
    aged.sort_unstable_by_key(|(seq, _)| *seq);

    let mut removed = 0usize;
    for (_, key) in aged {
        if inner.map.len() <= target {
            break;
        }
        inner.map.remove(&key);
        removed += 1;
    }
    // If even after sacrificing the aged half the map still exceeds
    // capacity (everything is NEWER than TTL/2 — a genuine insert flood),
    // fall back to dropping the oldest seqs regardless of age: memory
    // bounds are non-negotiable, ordering by seq keeps it deterministic.
    if inner.map.len() > capacity {
        let mut all: Vec<(u64, (String, String))> =
            inner.map.iter().map(|(k, e)| (e.seq, k.clone())).collect();
        all.sort_unstable_by_key(|(seq, _)| *seq);
        let excess = inner.map.len() - capacity;
        for (_, key) in all.into_iter().take(excess) {
            inner.map.remove(&key);
        }
        let _ = removed; // (kept for log parity on embedded callers)
    }
}
