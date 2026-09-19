package session

import (
	"crypto/rand"
	"fmt"
	"sync"
	"time"
)

// Event kinds: what the signaling layer must turn into wire frames. The
// session layer reports; the signaling layer renders. Keeping the rendering
// out of here is what makes the entire state machine testable with a plain
// []Event assertion.
type EventKind int

const (
	EvStarted          EventKind = iota // ack to the initiator
	EvJoined                            // ack to the responder
	EvPeerJoined                        // told to the member who was waiting
	EvEnded                             // told to each still-present member, with a reason
	EvForwardOffer                      // relay an SDP offer to the peer
	EvForwardAnswer                     // relay an SDP answer to the peer
	EvForwardCandidate                  // relay an ICE candidate blob to the peer
)

// Event is one outcome of a manager operation addressed to exactly one
// connection. Payload fields are populated by kind (SDP for offers/answers,
// Candidate for candidates, Reason for EvEnded, PeerRole for EvPeerJoined,
// Role for the acks).
type Event struct {
	Kind      EventKind
	Recipient string // connection id the reply is addressed to
	SessionID string
	Role      string
	PeerRole  string
	Reason    string
	SDP       string
	Candidate []byte
}

// Manager owns the session lifecycle for every tenant. One mutex serializes
// everything: sessions are tiny, contention is bounded by the per-tenant
// cap, and a single lock is the design that keeps the state machine free of
// ordering subtleties. Every method is safe for concurrent use.
type Manager struct {
	mu           sync.Mutex
	reg          *Registry
	maxPerTenant int
	pendingTTL   time.Duration

	// idgen/now are constructor-fixed seams (tests in this package replace
	// them) — determinism where a random id or a wall clock would make an
	// assertion flaky, nothing else.
	idgen func() (string, error)
	now   func() time.Time
}

// NewManager builds a manager with the production clock and ids.
func NewManager(maxPerTenant int, pendingTTL time.Duration) *Manager {
	return &Manager{
		reg:          NewRegistry(),
		maxPerTenant: maxPerTenant,
		pendingTTL:   pendingTTL,
		idgen:        newUUIDv4,
		now:          time.Now,
	}
}

// Start creates a pending session with connID as initiator. The returned id
// IS the join capability: 122 random bits, only ever exchanged between the
// member and its intended peer, meaningful solely inside the same tenant.
func (m *Manager) Start(tenantID, connID string) (string, []Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.reg.sessionForConn(connID); ok {
		return "", nil, ErrAlreadyInSession
	}
	if m.reg.countForTenant(tenantID) >= m.maxPerTenant {
		return "", nil, ErrTooManySessions
	}
	id, err := m.idgen()
	if err != nil {
		return "", nil, fmt.Errorf("generate session id: %w", err)
	}
	s := NewSession(id, tenantID, connID, "initiator", m.now())
	m.reg.insert(s)
	return id, []Event{{Kind: EvStarted, Recipient: connID, SessionID: id, Role: "initiator"}}, nil
}

// Join seats connID as a session's responder and wakes the initiator. A
// wrong id, a wrong tenant, or an ended session all return the same
// ErrUnknownSession — the join capability proves nothing about its subject
// until it succeeds.
func (m *Manager) Join(tenantID, sessionID, connID string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.reg.sessionForConn(connID); ok {
		return nil, ErrAlreadyInSession
	}
	s, ok := m.reg.get(sessionID)
	if !ok || s.TenantID != tenantID {
		return nil, ErrUnknownSession
	}
	if s.memberCount() >= 2 {
		// A point-to-point negotiation is exactly two seats; the third
		// arriver had the id but not the moment.
		return nil, ErrWrongState
	}
	initiator := s.members[0]
	s.members = append(s.members, Member{ConnID: connID, Role: "responder"})
	m.reg.bindConn(connID, s.ID)
	s.touchChange(m.now())
	return []Event{
		{Kind: EvJoined, Recipient: connID, SessionID: s.ID, Role: "responder"},
		{Kind: EvPeerJoined, Recipient: initiator.ConnID, SessionID: s.ID, PeerRole: "responder"},
	}, nil
}

// End terminates a session at a member's request. Both members (requester
// included) receive EvEnded — the sender's copy is the ack.
func (m *Manager) End(tenantID, sessionID, connID, reason string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	return m.endLocked(s, reason), nil
}

// Offer applies an SDP offer: legal from either member once both seats are
// filled, exactly one at a time (the Offering state is the glare guard).
func (m *Manager) Offer(tenantID, sessionID, connID, sdp string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	if s.memberCount() < 2 {
		return nil, ErrSessionNotReady
	}
	if s.State == Offering {
		// One outstanding offer at a time, full stop. Whoever the offender
		// is — even the original offerer re-offering — the state answers.
		return nil, ErrWrongState
	}
	me, _ := s.memberByConn(connID)
	s.State = Offering
	s.offerBy = me.Role
	s.touchChange(m.now())
	peer, _ := s.peerOf(connID)
	return []Event{{Kind: EvForwardOffer, Recipient: peer.ConnID, SessionID: s.ID, SDP: sdp}}, nil
}

// Answer completes an outstanding offer, from the member who did not make
// it. A "yes" to a question nobody asked is wrong_state, and so is the
// offerer answering itself.
func (m *Manager) Answer(tenantID, sessionID, connID, sdp string) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	if s.State != Offering {
		return nil, ErrWrongState
	}
	me, _ := s.memberByConn(connID)
	if me.Role == s.offerBy {
		return nil, ErrWrongState
	}
	s.State = Answered
	s.offerBy = ""
	s.touchChange(m.now())
	peer, _ := s.peerOf(connID)
	return []Event{{Kind: EvForwardAnswer, Recipient: peer.ConnID, SessionID: s.ID, SDP: sdp}}, nil
}

// Candidate relays one ICE candidate to the peer. Legal whenever both seats
// are filled (the browser's own stack drops anything that arrives before it
// can use it; the relay's concern is seating, not media sequencing).
func (m *Manager) Candidate(tenantID, sessionID, connID string, raw []byte) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, err := m.lookupMemberLocked(tenantID, sessionID, connID)
	if err != nil {
		return nil, err
	}
	if s.memberCount() < 2 {
		return nil, ErrSessionNotReady
	}
	peer, _ := s.peerOf(connID)
	return []Event{{Kind: EvForwardCandidate, Recipient: peer.ConnID, SessionID: s.ID, Candidate: raw}}, nil
}

// ConnDropped ends whatever session connID held a seat in, notifying the
// remaining member if there is one. Reconnect-and-renegotiate deliberately
// does not exist: sockets own session liveness, ICE restart is exactly as
// expensive as a fresh offer, and a design where zombies sessions outlive
// their sockets is a breach vector, not a feature.
func (m *Manager) ConnDropped(connID string) []Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	id, ok := m.reg.sessionForConn(connID)
	if !ok {
		return nil
	}
	s, ok := m.reg.get(id)
	if !ok {
		m.reg.unbindConn(connID)
		return nil
	}
	events := m.endLocked(s, ReasonPeerDisconnected)
	// The dropped connection gets no notice — it is already gone.
	out := events[:0]
	for _, ev := range events {
		if ev.Recipient != connID {
			out = append(out, ev)
		}
	}
	return out
}

// lookupMemberLocked resolves (tenant, session id, member) with the
// collapsed ErrUnknownSession for every negative case. Caller holds mu.
func (m *Manager) lookupMemberLocked(tenantID, sessionID, connID string) (*Session, error) {
	s, ok := m.reg.get(sessionID)
	if !ok || s.TenantID != tenantID {
		return nil, ErrUnknownSession
	}
	if _, ok := s.memberByConn(connID); !ok {
		// A stranger who somehow knows the id is not told the difference.
		return nil, ErrUnknownSession
	}
	return s, nil
}

// endLocked marks s ended, removes it from the registry, and returns one
// EvEnded per seat (present or vacated — the caller strips recipients that
// no longer exist). Caller holds mu.
func (m *Manager) endLocked(s *Session, reason string) []Event {
	s.markEnded(reason, m.now())
	m.reg.remove(s.ID)
	events := make([]Event, 0, len(s.members))
	for _, member := range s.members {
		events = append(events, Event{
			Kind: EvEnded, Recipient: member.ConnID, SessionID: s.ID, Reason: reason,
		})
	}
	return events
}

// SessionForConn is the public member-binding read the steer path needs:
// which session, if any, holds a seat for this connection right now.
func (m *Manager) SessionForConn(connID string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.reg.sessionForConn(connID)
}

// OtherMember resolves the peer's connection id within a session the
// CALLER has already verified it belongs to (membership proofs live
// behind the manager's own tenancy/membership checks — this helper
// answers peer-not-you, nothing more).
func (m *Manager) OtherMember(tenantID, sessionID, connID string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.reg.get(sessionID)
	if !ok || s.TenantID != tenantID {
		return "", false
	}
	for _, member := range s.members {
		if member.ConnID != connID {
			return member.ConnID, true
		}
	}
	return "", false
}

// Stats returns the gauge substrate for /metrics: live sessions and tenants
// holding at least one.
func (m *Manager) Stats() (sessions, tenants int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.reg.total(), m.reg.tenants()
}

// newUUIDv4 returns a random RFC 4122 v4 UUID — the same byte shape the
// websocket connection ids, the Python backend, signal-go and the Rust hub
// all emit. Duplicated deliberately (small, stable, and cheaper than a
// shared package for 12 lines of crypto/rand).
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
