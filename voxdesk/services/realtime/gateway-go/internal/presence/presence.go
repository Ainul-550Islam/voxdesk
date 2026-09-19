// Package presence tracks WHICH dashboard users are currently reachable
// through this gateway node, derived from the identities the WebSocket
// edge has already verified.
//
// Data sources: exactly two moments in the connection lifecycle — the
// successful hello (a JWT pinned a user to a socket) and the connection's
// teardown (the socket is gone). Presence never trusts a client-asserted
// identity: the only keys it ever records come out of a verified token, so
// "user U of tenant T is online here" is as trustworthy as the JWT itself.
//
// Scope is NODE-local by design (mirroring the hub's room registry): a
// multi-node deployment composes node-local views, the same way ingest
// fan-out composes through the broker. Nothing here pretends to be a
// global directory.
//
// Every method is safe for concurrent use.
package presence

import (
	"sort"
	"sync"
	"time"
)

// Registry is the presence store: tenant → user → set of live session ids.
// A user is "online" for a tenant while at least one of their sessions is;
// multiple tabs/devices of the same user collapse into one online entry,
// which is the answer every presence consumer actually wants.
type Registry struct {
	mu       sync.Mutex
	byTenant map[string]map[string]map[string]struct{} // tenant → user → sessions
	onChange []func(Event)
}

// New returns an empty registry.
func New() *Registry {
	return &Registry{byTenant: make(map[string]map[string]map[string]struct{})}
}

// Online records one live session for (tenant, user) and reports the
// resulting transition. Idempotent per session: re-recording the same
// (tenant, user, session) triple is a no-op event with Event.Changed=false,
// so a duplicated delivery path cannot inflate session counts. Empty
// tenant/user keys are ignored entirely — presence of an unauthenticated
// socket is not presence.
func (r *Registry) Online(tenantID, userID, sessionID string) Event {
	if tenantID == "" || userID == "" || sessionID == "" {
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	r.mu.Lock()
	users, ok := r.byTenant[tenantID]
	if !ok {
		users = make(map[string]map[string]struct{})
		r.byTenant[tenantID] = users
	}
	sessions, ok := users[userID]
	if !ok {
		sessions = make(map[string]struct{})
		users[userID] = sessions
	}
	_, existed := sessions[sessionID]
	sessions[sessionID] = struct{}{}
	event := Event{
		Kind:         KindUnchanged,
		TenantID:     tenantID,
		UserID:       userID,
		SessionID:    sessionID,
		At:           time.Now(),
		UserSessions: len(sessions),
		TenantUsers:  len(users),
		TotalUsers:   r.totalUsersLocked(),
	}
	if !existed {
		event.Kind = KindSessionJoined
		if len(sessions) == 1 {
			event.Kind = KindUserOnline // FIRST session: the user became reachable
		}
	}
	r.mu.Unlock()
	r.emit(event)
	return event
}

// Offline drops one session. Reports the transition; a no-op
// (Event.Kind == KindUnchanged, without callbacks) when the triple was
// never recorded, so shutdown sweeps racing connection teardown cannot
// double-decrement anything.
func (r *Registry) Offline(tenantID, userID, sessionID string) Event {
	r.mu.Lock()
	users, ok := r.byTenant[tenantID]
	if !ok {
		r.mu.Unlock()
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	sessions, ok := users[userID]
	if !ok {
		r.mu.Unlock()
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	if _, existed := sessions[sessionID]; !existed {
		r.mu.Unlock()
		return Event{Kind: KindUnchanged, At: time.Now()}
	}
	delete(sessions, sessionID)
	event := Event{
		Kind:      KindSessionLeft,
		TenantID:  tenantID,
		UserID:    userID,
		SessionID: sessionID,
		At:        time.Now(),
	}
	if len(sessions) == 0 {
		delete(users, userID)
		event.Kind = KindUserOffline // LAST session gone: user no longer reachable
	}
	if len(users) == 0 {
		delete(r.byTenant, tenantID)
	}
	event.UserSessions = len(sessions)
	event.TenantUsers = len(users)
	event.TotalUsers = r.totalUsersLocked()
	r.mu.Unlock()
	r.emit(event)
	return event
}

// IsOnline reports whether the user has at least one live session.
func (r *Registry) IsOnline(tenantID, userID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.byTenant[tenantID][userID]) > 0
}

// SessionsOf returns the live session ids of (tenant, user), sorted —
// deterministic output for diagnostics and tests.
func (r *Registry) SessionsOf(tenantID, userID string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	sessions := r.byTenant[tenantID][userID]
	out := make([]string, 0, len(sessions))
	for sessionID := range sessions {
		out = append(out, sessionID)
	}
	sort.Strings(out)
	return out
}

// OnlineUsers returns the tenant's reachable user ids, sorted.
func (r *Registry) OnlineUsers(tenantID string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	users := r.byTenant[tenantID]
	out := make([]string, 0, len(users))
	for userID := range users {
		out = append(out, userID)
	}
	sort.Strings(out)
	return out
}

// TenantCount is how many tenants have at least one reachable user.
func (r *Registry) TenantCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.byTenant)
}

// TotalUsers is the node-wide count of reachable (tenant, user) pairs —
// the number the /metrics presence gauge renders. Cardinality-safe: it is
// ONE gauge, not a label dimension.
func (r *Registry) TotalUsers() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.totalUsersLocked()
}
