package session

// Registry is the manager's indexed store. It holds no lock of its own —
// the Manager serializes everything — because a lock here plus a lock there
// would be two lock orderings to reason about, and the value of the lock
// hierarchy being trivial dwarfs any read-throughput fantasy a signaling
// plane with ≤ 64 sessions per tenant could have.
type Registry struct {
	byID     map[string]*Session
	byTenant map[string]map[string]struct{}
	byConn   map[string]string // connection id → session id (≤ 1 session per connection)
}

// NewRegistry returns an empty store.
func NewRegistry() *Registry {
	return &Registry{
		byID:     make(map[string]*Session),
		byTenant: make(map[string]map[string]struct{}),
		byConn:   make(map[string]string),
	}
}

// get returns the session by id.
func (r *Registry) get(id string) (*Session, bool) {
	s, ok := r.byID[id]
	return s, ok
}

// countForTenant is the denominator of the per-tenant session cap.
func (r *Registry) countForTenant(tenantID string) int {
	return len(r.byTenant[tenantID])
}

// total reports live sessions process-wide (a gauge substrate; per-tenant
// snapshots are available through Stats on the manager).
func (r *Registry) total() int { return len(r.byID) }

// tenantSessionCount reports per-tenant occupancy for metrics snapshots.
func (r *Registry) tenants() int { return len(r.byTenant) }

// sessionForConn maps a connection to its session, if any.
func (r *Registry) sessionForConn(connID string) (string, bool) {
	id, ok := r.byConn[connID]
	return id, ok
}

// insert registers a new session and every member seat it already holds.
func (r *Registry) insert(s *Session) {
	r.byID[s.ID] = s
	if r.byTenant[s.TenantID] == nil {
		r.byTenant[s.TenantID] = make(map[string]struct{})
	}
	r.byTenant[s.TenantID][s.ID] = struct{}{}
	for _, m := range s.members {
		r.byConn[m.ConnID] = s.ID
	}
}

// bindConn registers one seat's membership (used when the responder joins;
// creation-time members are covered by insert).
func (r *Registry) bindConn(connID, sessionID string) {
	r.byConn[connID] = sessionID
}

// remove drops the session and every member binding. Dropping a missing id
// is a no-op — a 2-member session can only end once, but the callers that
// tear down (end, disconnect, reaper) must each be safe to lose a race.
func (r *Registry) remove(id string) {
	s, ok := r.byID[id]
	if !ok {
		return
	}
	delete(r.byID, id)
	if set, ok := r.byTenant[s.TenantID]; ok {
		delete(set, id)
		if len(set) == 0 {
			delete(r.byTenant, s.TenantID)
		}
	}
	for _, m := range s.members {
		// Only unbind if THIS session owns the binding; a connection whose
		// session ended normally already unbound via unbindConn.
		if cur, ok := r.byConn[m.ConnID]; ok && cur == id {
			delete(r.byConn, m.ConnID)
		}
	}
}

// unbindConn releases one connection's seat binding (when it leaves an
// otherwise-live session — today always followed by the session ending, so
// this exists for completeness of the index invariant).
func (r *Registry) unbindConn(connID string) {
	delete(r.byConn, connID)
}

// each calls fn for every live session, in map order. Callers must not rely
// on iteration order (a unit test asserting order is a flaky test by
// definition) and must not call back into the registry (the manager's lock
// is held).
func (r *Registry) each(fn func(*Session)) {
	for _, s := range r.byID {
		fn(s)
	}
}
