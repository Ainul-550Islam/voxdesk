package session

import "time"

// This file owns TIME against sessions: the policy of when a session that
// never became a negotiation must stop waiting, and the sweep that applies
// it.
//
// Only ONE shape of time-decay is reaped here: a session that was started
// but never joined (one member, Pending) past pendingTTL. Everything else
// owes its liveness to its sockets — an Offering session whose answerer
// walked away is not this layer's problem, because the websocket heartbeat
// will report the socket dead and ConnDropped ends the session on evidence,
// not on a timer. Timeouts guess; disconnections are facts.

// PendingTTL exposes the configured lone-session timeout.
func (m *Manager) PendingTTL() time.Duration { return m.pendingTTL }

// ReapInterval is the recommended cadence for the reaper loop: half the
// timeout (so a lone session is reaped within 1–1.5× TTL), clamped away
// from nonsense. The caller MAY tick slower; ticking faster than 1 s buys
// nothing.
func (m *Manager) ReapInterval() time.Duration {
	half := m.pendingTTL / 2
	if half < time.Second {
		return time.Second
	}
	return half
}

// Reap ends every lone, pending, over-age session and returns the events to
// notify the affected members. Thread-safe; intended to be called on a
// ticker by the signaling layer (which owns frame emission).
func (m *Manager) Reap() []Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()

	// Collect first, mutate after: removing from the registry while its
	// each() iterates is the classic iterator-invalidation crash, and a
	// leftover-slice beats a cleverer loop that trips over it once a year.
	var expired []*Session
	m.reg.each(func(s *Session) {
		if s.State == Pending && s.memberCount() == 1 && now.Sub(s.created()) >= m.pendingTTL {
			expired = append(expired, s)
		}
	})
	var events []Event
	for _, s := range expired {
		events = append(events, m.endLocked(s, ReasonJoinTimeout)...)
	}
	return events
}
