//! Exactly-once guard for cross-service events.
//!
//! Mirrors the database-level `UniqueConstraint(tenant_id, idempotency_key)`
//! guarantee (see `alembic/versions/0011_side_effect_exactly_once.py` and the
//! CRM / billing / appointment idempotency columns): a redelivery of an event
//! already applied for `(tenant, key)` is a no-op.

use std::collections::HashSet;
use std::sync::Mutex;

#[derive(Debug, Default)]
pub struct IdempotencyGuard {
    seen: Mutex<HashSet<(String, String)>>,
}

impl IdempotencyGuard {
    pub fn new() -> Self {
        IdempotencyGuard {
            seen: Mutex::new(HashSet::new()),
        }
    }

    /// Returns `true` the first time `(tenant, key)` is applied, `false` on
    /// every subsequent application (the caller must treat `false` as
    /// "already done; do not perform the side effect again").
    pub fn apply(&self, tenant: &str, key: &str) -> bool {
        self.seen
            .lock()
            .unwrap()
            .insert((tenant.to_string(), key.to_string()))
    }

    pub fn contains(&self, tenant: &str, key: &str) -> bool {
        self.seen
            .lock()
            .unwrap()
            .contains(&(tenant.to_string(), key.to_string()))
    }

    pub fn len(&self) -> usize {
        self.seen.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_apply_wins() {
        let guard = IdempotencyGuard::new();
        assert!(guard.apply("tenant-a", "event-1"));
        assert!(!guard.apply("tenant-a", "event-1"));
        assert_eq!(guard.len(), 1);
    }

    #[test]
    fn same_key_different_tenant_is_independent() {
        let guard = IdempotencyGuard::new();
        assert!(guard.apply("tenant-a", "event-1"));
        assert!(guard.apply("tenant-b", "event-1"));
        assert_eq!(guard.len(), 2);
    }
}
