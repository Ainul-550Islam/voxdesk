// Package hub is the tenant-scoped connection registry and fan-out core of
// the public edge.
//
// It mirrors the internal hub's (services/signal-go/internal/signal/hub.go)
// structural guarantee — a room is keyed by (tenant_id, room), so a delivery
// can NEVER cross a tenant boundary by construction, not by a remembered
// where-clause — and adds the two things a PUBLIC edge must enforce that an
// internal service trusts its network for:
//
//   - a per-tenant connection cap, so one tenant's traffic (or one leaked
//     token) cannot consume the whole gateway;
//   - backpressure-drop with an explicit dropped count: a slow browser must
//     never be able to stall a room, and the drop must be observable rather
//     than silent (mirrors the Rust BoundedBroadcast contract).
//
// Every method is safe for concurrent use.
package hub

import (
	"encoding/json"
	"errors"
	"sync"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// Subscriber is the transport-agnostic view of one live connection.
// Implemented by internal/websocket.Connection; tests use a recorder.
type Subscriber interface {
	// Session returns the server-assigned session id (unique per connection).
	Session() string
	// Tenant returns the pinned tenant id, or "" before authentication.
	Tenant() string
	// Enqueue offers one frame to the connection's bounded outgoing queue.
	// It reports false when the queue is full (backpressure-drop) and must
	// never block.
	Enqueue(msg any) bool
	// RequestClose asks the transport to close with an RFC 6455 code/reason.
	// Used for administrative closes (shutdown, token expiry); it must be
	// idempotent.
	RequestClose(code int, reason string)
}

// ErrOverTenantCap is returned by BindTenant when the tenant is already at
// its connection ceiling.
var ErrOverTenantCap = errors.New("tenant connection cap reached")

// ErrUnknownSession identifies bookkeeping calls for a session that is not
// (or no longer) registered; treated as a no-op by callers, but distinguishable.
var ErrUnknownSession = errors.New("unknown session")

// roomKey is a (tenant_id, room) pair — the room name alone is never a key.
type roomKey struct {
	tenantID string
	room     string
}

// sessionMeta is the hub's bookkeeping for one live connection.
type sessionMeta struct {
	sub   Subscriber
	rooms map[string]struct{}
}

// Hub is the shared registry. A single mutex guards all maps; fan-out
// builds its recipient list under the lock and enqueues outside it, so a
// wedged Subscriber (which Enqueue must never be, by contract) cannot stall
// other tenants.
type Hub struct {
	mu sync.Mutex

	sessions map[string]*sessionMeta
	rooms    map[roomKey]map[string]struct{}
	// tenantConns counts AUTHENTICATED connections per tenant — the
	// denominator of the per-tenant cap. Unauthenticated sockets count only
	// against the global cap enforced at upgrade time.
	tenantConns map[string]int

	maxPerTenant int
}

// New returns an empty Hub. maxPerTenant is the per-tenant connection cap.
func New(maxPerTenant int) *Hub {
	return &Hub{
		sessions:     make(map[string]*sessionMeta),
		rooms:        make(map[roomKey]map[string]struct{}),
		tenantConns:  make(map[string]int),
		maxPerTenant: maxPerTenant,
	}
}

// Register records a new, not-yet-authenticated connection.
func (h *Hub) Register(sub Subscriber) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.sessions[sub.Session()] = &sessionMeta{sub: sub, rooms: make(map[string]struct{})}
}

// BindTenant pins a session to its verified tenant after authentication and
// enforces the per-tenant cap. It is intentionally idempotent per session:
// re-binding to the SAME tenant succeeds quietly (a client retrying hello
// after a flaky frame), while binding to a DIFFERENT tenant than the one
// pinned is refused.
func (h *Hub) BindTenant(sessionID, tenantID string) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return ErrUnknownSession
	}
	if current := meta.sub.Tenant(); current != "" {
		if current == tenantID {
			return nil
		}
		return ErrUnknownSession // wrong-tenant rebind must not reveal which tenants are live
	}
	if h.tenantConns[tenantID] >= h.maxPerTenant {
		return ErrOverTenantCap
	}
	h.tenantConns[tenantID]++
	return nil
}

// Lookup resolves a connection id to its live subscriber. The signaling
// relay addresses frames by connection id and needs reads without holding
// the hub's lock across an Enqueue — exactly the Publish pattern: snapshot
// under lock, touch the transport outside it.
func (h *Hub) Lookup(sessionID string) (Subscriber, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return nil, false
	}
	return meta.sub, true
}

// Unregister drops the session, every room membership it held, and its
// tenant slot. It is a no-op for an unknown session (a close racing a
// shutdown sweep must not panic or corrupt counts).
func (h *Hub) Unregister(sessionID string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return
	}
	delete(h.sessions, sessionID)
	tenantID := meta.sub.Tenant()
	if tenantID != "" {
		if h.tenantConns[tenantID] <= 1 {
			delete(h.tenantConns, tenantID)
		} else {
			h.tenantConns[tenantID]--
		}
	}
	for roomName := range meta.rooms {
		key := roomKey{tenantID: tenantID, room: roomName}
		if peers, ok := h.rooms[key]; ok {
			delete(peers, sessionID)
			if len(peers) == 0 {
				delete(h.rooms, key)
			}
		}
	}
}

// Subscribe joins a session to (its own tenant, room) and returns the
// room's peer count AFTER the join. The tenant comes from the session's
// pin, not from the message — the frame schema cannot express a tenant.
func (h *Hub) Subscribe(sessionID, room string) (int, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return 0, ErrUnknownSession
	}
	tenantID := meta.sub.Tenant()
	if tenantID == "" {
		return 0, ErrUnknownSession
	}
	meta.rooms[room] = struct{}{}
	key := roomKey{tenantID: tenantID, room: room}
	peers, ok := h.rooms[key]
	if !ok {
		peers = make(map[string]struct{})
		h.rooms[key] = peers
	}
	peers[sessionID] = struct{}{}
	return len(peers), nil
}

// Unsubscribe removes a membership. Leaving a room never joined is a
// successful no-op (mirrors the internal hub: idempotent, never an error,
// so a client that lost track of its own state during a reconnect can just
// re-assert the state it wants).
func (h *Hub) Unsubscribe(sessionID, room string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return
	}
	tenantID := meta.sub.Tenant()
	delete(meta.rooms, room)
	key := roomKey{tenantID: tenantID, room: room}
	if peers, ok := h.rooms[key]; ok {
		delete(peers, sessionID)
		if len(peers) == 0 {
			delete(h.rooms, key)
		}
	}
}

// Publish fans one event out to every subscriber of (tenantID, room).
// Returns (delivered, dropped): delivered frames reached a subscriber's
// queue, dropped frames found it full. NOTHING outside the target room is
// touched, and a tenant id that is not a UUID never reaches this function
// (ingest validates shapes first).
func (h *Hub) Publish(tenantID, room, kind, eventID string, payload json.RawMessage) (delivered, dropped int) {
	h.mu.Lock()
	key := roomKey{tenantID: tenantID, room: room}
	var targets []Subscriber
	if peers, ok := h.rooms[key]; ok {
		for sessionID := range peers {
			if meta, ok := h.sessions[sessionID]; ok {
				targets = append(targets, meta.sub)
			}
		}
	}
	h.mu.Unlock()

	if len(targets) == 0 {
		return 0, 0
	}
	frame := protocol.NewDelivery(room, kind, eventID, payload, time.Now())
	for _, sub := range targets {
		if sub.Enqueue(frame) {
			delivered++
		} else {
			dropped++
		}
	}
	return delivered, dropped
}

// PeerCount reports the subscriber count of (tenantID, room) — diagnostics
// for health and for tests; not exposed per-tenant on /metrics (cardinality).
func (h *Hub) PeerCount(tenantID, room string) int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.rooms[roomKey{tenantID: tenantID, room: room}])
}

// Stats returns the gauges the /metrics exposition and readiness checks
// render: rooms with members, tenants with live connections.
func (h *Hub) Stats() (rooms int, tenants int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.rooms), len(h.tenantConns)
}

// CloseAll asks every live connection to close with the given code/reason.
// Called once on shutdown; draining is the connections' own concern, so a
// browser that ignores the close frame cannot hold the process open past
// its shutdown deadline.
func (h *Hub) CloseAll(code int, reason string) {
	h.mu.Lock()
	targets := make([]Subscriber, 0, len(h.sessions))
	for _, meta := range h.sessions {
		targets = append(targets, meta.sub)
	}
	h.mu.Unlock()
	for _, sub := range targets {
		sub.RequestClose(code, reason)
	}
}
