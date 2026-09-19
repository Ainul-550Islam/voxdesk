package signal

import (
	"encoding/json"
	"sync"
)

// roomKey is a (tenant_id, room) pair. The room name alone is never a key,
// which is what makes cross-tenant delivery structurally impossible.
type roomKey struct {
	tenantID string
	room     string
}

// room is the set of session IDs subscribed to one (tenant, room).
type room struct {
	peers map[string]struct{}
}

// sessionMeta is the hub's bookkeeping for one live connection.
type sessionMeta struct {
	tenantID string
	rooms    map[string]struct{}
	outgoing chan ServerMessage
}

// Hub is the shared registry and fan-out. Every method is safe for
// concurrent use. Outgoing channels are never closed: a session that is
// removed simply drops out of the map and the channel is garbage-collected,
// so a racing publish can never panic on a closed channel — it just drops.
type Hub struct {
	mu       sync.Mutex
	rooms    map[roomKey]*room
	sessions map[string]*sessionMeta
}

func NewHub() *Hub {
	return &Hub{
		rooms:    make(map[roomKey]*room),
		sessions: make(map[string]*sessionMeta),
	}
}

// outgoingBuffer is the per-session outbound queue. A slow client that fills
// its queue is skipped (backpressure-drop) rather than blocking the sender.
const outgoingBuffer = 64

// register records a new session and returns its buffered outgoing channel.
func (h *Hub) register(sessionID string) chan ServerMessage {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make(chan ServerMessage, outgoingBuffer)
	h.sessions[sessionID] = &sessionMeta{
		rooms:    make(map[string]struct{}),
		outgoing: out,
	}
	return out
}

// removeSession drops the session and every room membership it held, pruning
// rooms that become empty. It is a no-op for an unknown session.
func (h *Hub) removeSession(sessionID string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return
	}
	delete(h.sessions, sessionID)
	if meta.tenantID == "" {
		return
	}
	for roomName := range meta.rooms {
		key := roomKey{tenantID: meta.tenantID, room: roomName}
		if roomState, ok := h.rooms[key]; ok {
			delete(roomState.peers, sessionID)
			if len(roomState.peers) == 0 {
				delete(h.rooms, key)
			}
		}
	}
}

// hello records the tenant for a session. It succeeds exactly once with a
// non-empty tenant; a second hello (or an empty tenant) yields hello_invalid.
// On success there is no reply frame, matching the Rust hub.
func (h *Hub) hello(sessionID, tenantID string) (ServerMessage, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return nil, false
	}
	if meta.tenantID != "" || tenantID == "" {
		return newError(CodeHelloInvalid, "hello may be sent exactly once with a non-empty tenant_id"), true
	}
	meta.tenantID = tenantID
	return nil, false
}

// subscribe adds the session to (tenant, room) and reports the new peer
// count. A session that has not said hello is refused with hello_required.
func (h *Hub) subscribe(sessionID, roomName string) (ServerMessage, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return nil, false
	}
	if meta.tenantID == "" {
		return newError(CodeHelloRequired, "send hello before subscribing"), true
	}
	meta.rooms[roomName] = struct{}{}
	key := roomKey{tenantID: meta.tenantID, room: roomName}
	roomState, ok := h.rooms[key]
	if !ok {
		roomState = &room{peers: make(map[string]struct{})}
		h.rooms[key] = roomState
	}
	roomState.peers[sessionID] = struct{}{}
	return newSubscribed(roomName, len(roomState.peers)), true
}

// unsubscribe removes the session from (tenant, room) and prunes the room if
// it emptied. Without a hello it is silently ignored, mirroring the Rust hub.
func (h *Hub) unsubscribe(sessionID, roomName string) (ServerMessage, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		return nil, false
	}
	if meta.tenantID == "" {
		return nil, false
	}
	delete(meta.rooms, roomName)
	key := roomKey{tenantID: meta.tenantID, room: roomName}
	if roomState, ok := h.rooms[key]; ok {
		delete(roomState.peers, sessionID)
		if len(roomState.peers) == 0 {
			delete(h.rooms, key)
		}
	}
	return newUnsubscribed(roomName), true
}

// publish fans a payload out to every other peer in the sender's room within
// the sender's tenant. Delivery is non-blocking: a peer whose outgoing buffer
// is full is skipped rather than slowing the sender down, and the sender
// never receives its own payload. It returns the number of peers reached.
func (h *Hub) publish(sessionID, roomName string, payload json.RawMessage) int {
	h.mu.Lock()
	meta, ok := h.sessions[sessionID]
	if !ok {
		h.mu.Unlock()
		return 0
	}
	if meta.tenantID == "" {
		out := meta.outgoing
		h.mu.Unlock()
		trySend(out, newError(CodeHelloRequired, "send hello before publishing"))
		return 0
	}
	roomState, ok := h.rooms[roomKey{tenantID: meta.tenantID, room: roomName}]
	if !ok {
		h.mu.Unlock()
		return 0
	}
	peers := make([]chan ServerMessage, 0, len(roomState.peers))
	for peer := range roomState.peers {
		if peer == sessionID {
			continue
		}
		if peerMeta, ok := h.sessions[peer]; ok {
			peers = append(peers, peerMeta.outgoing)
		}
	}
	h.mu.Unlock()

	delivery := newDelivery(roomName, sessionID, payload)
	delivered := 0
	for _, out := range peers {
		if trySend(out, delivery) {
			delivered++
		}
	}
	return delivered
}

// sessionCount reports the number of live sessions (health checks, tests).
func (h *Hub) sessionCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.sessions)
}

// roomPeers reports how many peers are subscribed to (tenant, room).
func (h *Hub) roomPeers(tenantID, roomName string) int {
	h.mu.Lock()
	defer h.mu.Unlock()
	if roomState, ok := h.rooms[roomKey{tenantID: tenantID, room: roomName}]; ok {
		return len(roomState.peers)
	}
	return 0
}

// trySend is a non-blocking channel send; it returns false (drops) when the
// buffer is full. The send-only signature accepts both `chan ServerMessage`
// and `chan<- ServerMessage`.
func trySend(ch chan<- ServerMessage, msg ServerMessage) bool {
	select {
	case ch <- msg:
		return true
	default:
		return false
	}
}
