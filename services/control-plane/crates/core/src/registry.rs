//! Tenant-scoped concurrent registry.
//!
//! The isolation primitive: a resource id is only ever addressable together
//! with the tenant that owns it. There is no `get(id)` without a tenant, so a
//! caller that forgets the tenant cannot compile the mistake — the same rule
//! the HTTP API enforces (the tenant binding comes from the authenticated
//! principal, never from a bare id).

use std::collections::HashMap;
use std::hash::Hash;
use std::sync::RwLock;

#[derive(Debug, Default)]
pub struct TenantRegistry<T, I>
where
    T: Eq + Hash + Clone,
    I: Eq + Hash + Clone,
{
    inner: RwLock<HashMap<(T, I), ()>>,
}

impl<T, I> TenantRegistry<T, I>
where
    T: Eq + Hash + Clone,
    I: Eq + Hash + Clone + Ord,
{
    pub fn new() -> Self {
        TenantRegistry {
            inner: RwLock::new(HashMap::new()),
        }
    }

    /// Insert `(tenant, id)`. Returns `true` when newly inserted, `false` when
    /// it already existed.
    pub fn insert(&self, tenant: T, id: I) -> bool {
        self.inner
            .write()
            .unwrap()
            .insert((tenant, id), ())
            .is_none()
    }

    pub fn contains(&self, tenant: &T, id: &I) -> bool {
        self.inner
            .read()
            .unwrap()
            .contains_key(&(tenant.clone(), id.clone()))
    }

    /// Remove `(tenant, id)`. Returns `true` when it was present.
    pub fn remove(&self, tenant: &T, id: &I) -> bool {
        self.inner
            .write()
            .unwrap()
            .remove(&(tenant.clone(), id.clone()))
            .is_some()
    }

    pub fn len(&self) -> usize {
        self.inner.read().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The number of distinct tenants.
    pub fn tenant_count(&self) -> usize {
        let inner = self.inner.read().unwrap();
        let tenants: std::collections::HashSet<T> = inner.keys().map(|(t, _)| t.clone()).collect();
        tenants.len()
    }

    /// Every id owned by `tenant`, sorted for determinism.
    pub fn ids_in_tenant(&self, tenant: &T) -> Vec<I> {
        let inner = self.inner.read().unwrap();
        let mut ids: Vec<I> = inner
            .keys()
            .filter(|(t, _)| t == tenant)
            .map(|(_, id)| id.clone())
            .collect();
        ids.sort();
        ids
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_contains_remove() {
        let reg: TenantRegistry<String, String> = TenantRegistry::new();
        assert!(reg.insert("tenant-a".into(), "call-1".into()));
        assert!(!reg.insert("tenant-a".into(), "call-1".into()));
        assert!(reg.contains(&"tenant-a".into(), &"call-1".into()));
        assert!(reg.remove(&"tenant-a".into(), &"call-1".into()));
        assert!(!reg.contains(&"tenant-a".into(), &"call-1".into()));
    }

    #[test]
    fn same_id_two_tenants_is_allowed() {
        let reg: TenantRegistry<String, String> = TenantRegistry::new();
        assert!(reg.insert("tenant-a".into(), "call-1".into()));
        assert!(reg.insert("tenant-b".into(), "call-1".into()));
        assert_eq!(reg.len(), 2);
        assert_eq!(reg.tenant_count(), 2);
    }

    #[test]
    fn ids_are_scoped_to_their_tenant() {
        let reg: TenantRegistry<String, String> = TenantRegistry::new();
        reg.insert("tenant-a".into(), "call-1".into());
        reg.insert("tenant-a".into(), "call-2".into());
        reg.insert("tenant-b".into(), "call-3".into());

        let a = reg.ids_in_tenant(&"tenant-a".into());
        assert_eq!(a, vec!["call-1".to_string(), "call-2".to_string()]);
        let b = reg.ids_in_tenant(&"tenant-b".into());
        assert_eq!(b, vec!["call-3".to_string()]);
    }
}
