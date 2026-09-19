package session

import (
	"testing"
	"time"
)

// The reaper's ONLY job is lone, pending, over-age sessions. Everything else
// is socket-owned liveness — hence the negatives below matter as much as the
// positive: a reaper that ever ended a live negotiation would be a bug the
// heartbeat already solves honestly.

func TestReapEndsLoneOverdueSessionsWithJoinTimeout(t *testing.T) {
	m, advance := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	advance(30 * time.Second) // under TTL
	if events := m.Reap(); len(events) != 0 {
		t.Fatalf("reaped early: %+v", events)
	}

	advance(31 * time.Second) // past the 60 s TTL
	events := m.Reap()
	if len(events) != 1 {
		t.Fatalf("reap events = %+v", events)
	}
	ev := events[0]
	if ev.Kind != EvEnded || ev.Recipient != connA1 || ev.Reason != ReasonJoinTimeout || ev.SessionID != id {
		t.Errorf("reap event = %+v", ev)
	}
	if sessions, _ := m.Stats(); sessions != 0 {
		t.Fatalf("reaped session still counted: %d", sessions)
	}

	// Reaping is idempotent — a second sweep sees nothing to do.
	if events := m.Reap(); len(events) != 0 {
		t.Fatalf("second reap = %+v", events)
	}
}

func TestReapLeavesJoinedAndNegotiatingSessionsAlone(t *testing.T) {
	m, advance := newTestManager(8, time.Minute)
	joined := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, joined, connA2, connA1)
	negotiating := mustStart(t, m, tenantA, connA3)
	mustJoin(t, m, tenantA, negotiating, connA4, connA3)
	mustOffer(t, m, tenantA, negotiating, connA3)

	advance(2 * time.Hour)
	if events := m.Reap(); len(events) != 0 {
		t.Fatalf("live sessions must NEVER be reaped, got %+v", events)
	}
}

func TestReapIntervalIsHalfTheTTLWithASaneFloor(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	if got := m.ReapInterval(); got != 30*time.Second {
		t.Errorf("ReapInterval = %v, want 30s", got)
	}

	tiny := NewManager(8, 500*time.Millisecond)
	if got := tiny.ReapInterval(); got != time.Second {
		t.Errorf("ReapInterval floor = %v, want 1s", got)
	}
}
