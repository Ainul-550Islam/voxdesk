package session

import (
	"errors"
	"time"
)

// Sentinel errors of the session layer. Callers map them onto protocol
// error codes (internal/signaling/events.go); sessions themselves never
// speak wire formats. Some collapses are SECURITY decisions, not laziness —
// see the comments on each sentinel.
var (
	// ErrUnknownSession: no such session for this tenant — including "it
	// exists under ANOTHER tenant" and "you are not a member of it". The
	// three cases share one answer so a probing client cannot enumerate
	// session ids, tenants, or memberships.
	ErrUnknownSession = errors.New("unknown session")
	// ErrTooManySessions: the tenant is at its concurrent-session cap.
	ErrTooManySessions = errors.New("too many sessions for tenant")
	// ErrAlreadyInSession: this connection already holds a session slot.
	ErrAlreadyInSession = errors.New("connection already in a session")
	// ErrSessionNotReady: the action requires two members; the peer slot is
	// still empty (an offer sent before anyone joined).
	ErrSessionNotReady = errors.New("session not ready for members")
	// ErrSessionFull: the second member slot is already taken — point to
	// point means exactly two, never a room.
	ErrSessionFull = errors.New("session already has two members")
	// ErrWrongState: legal message, illegal phase (glare: an offer while
	// one is outstanding; an answer with nothing outstanding).
	ErrWrongState = errors.New("wrong state for this message")
)

// Closed reason vocabulary, sent on the wire as session.ended.reason.
const (
	ReasonMemberEnded      = "member_ended"      // a member sent session.end
	ReasonPeerDisconnected = "peer_disconnected" // a member's socket died
	ReasonJoinTimeout      = "join_timeout"      // lifecycle reaped a lone pending session
)

// Member is one connection's seat in a session.
type Member struct {
	// ConnID is the connection's hub session id — the only identity the
	// session layer needs, and deliberately NOT a user or device id (those
	// have no business on a media negotiation ledger).
	ConnID string
	Role   string // protocol.RoleInitiator / protocol.RoleResponder, as a string to keep this package protocol-free
}

// Session is the negotiation ledger between two connections. Guarded by the
// OWNING Manager's mutex (see manager.go) — methods are plain and document
// their preconditions rather than re-locking, which keeps the state machine
// readable and free of a second lock hierarchy.
type Session struct {
	ID       string
	TenantID string
	State    State

	// members holds at most two seats: [0] is always the initiator, [1]
	// (when present) the responder.
	members []Member
	// offerBy is the role holding the outstanding offer, meaningful exactly
	// when State == Offering.
	offerBy   string
	createdAt time.Time
	changedAt time.Time
	endedAt   time.Time
	endReason string
}

// NewSession builds a pending session with one member (the initiator).
func NewSession(id, tenantID, connID, initiatorRole string, now time.Time) *Session {
	return &Session{
		ID:        id,
		TenantID:  tenantID,
		State:     Pending,
		members:   []Member{{ConnID: connID, Role: initiatorRole}},
		createdAt: now,
		changedAt: now,
	}
}

// memberByConn returns the member seat for connID, if any.
func (s *Session) memberByConn(connID string) (Member, bool) {
	for _, m := range s.members {
		if m.ConnID == connID {
			return m, true
		}
	}
	return Member{}, false
}

// peerOf returns the OTHER member — the forward destination for anything
// connID sends. With at most two members this is exhaustive.
func (s *Session) peerOf(connID string) (Member, bool) {
	for _, m := range s.members {
		if m.ConnID != connID {
			return m, true
		}
	}
	return Member{}, false
}

// memberCount reports occupied seats.
func (s *Session) memberCount() int { return len(s.members) }

// created/lastChanged expose the timestamps lifecycle.go's reaper needs.
func (s *Session) created() time.Time     { return s.createdAt }
func (s *Session) lastChanged() time.Time { return s.changedAt }

// touchChange marks a state-affecting event (membership or SDP).
func (s *Session) touchChange(now time.Time) { s.changedAt = now }

// markEnded seals the session; idempotent via the State check by callers.
func (s *Session) markEnded(reason string, now time.Time) {
	s.State = Ended
	s.endReason = reason
	s.endedAt = now
	s.changedAt = now
}
