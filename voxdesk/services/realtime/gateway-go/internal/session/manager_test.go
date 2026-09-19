package session

import (
	"errors"
	"fmt"
	"testing"
	"time"
)

const (
	tenantA = "11111111-1111-1111-1111-111111111111"
	tenantB = "22222222-2222-2222-2222-222222222222"
	connA1  = "conn-a-1"
	connA2  = "conn-a-2"
	connA3  = "conn-a-3"
	connA4  = "conn-a-4"
	connB1  = "conn-b-1"
)

// newTestManager returns a manager with deterministic ids (seq-1, seq-2, …)
// and a hand-cranked clock, so every assertion reads like a fact, not a
// race.
func newTestManager(maxPerTenant int, pendingTTL time.Duration) (*Manager, func(d time.Duration)) {
	seq := 0
	now := time.Date(2026, 9, 16, 10, 0, 0, 0, time.UTC)
	m := NewManager(maxPerTenant, pendingTTL)
	m.idgen = func() (string, error) {
		seq++
		return fmt.Sprintf("sig-%d", seq), nil
	}
	m.now = func() time.Time { return now }
	return m, func(d time.Duration) { now = now.Add(d) }
}

func mustStart(t *testing.T, m *Manager, tenant, conn string) string {
	t.Helper()
	id, events, err := m.Start(tenant, conn)
	if err != nil {
		t.Fatalf("Start(%s): %v", conn, err)
	}
	if len(events) != 1 || events[0].Kind != EvStarted || events[0].Recipient != conn || events[0].Role != "initiator" {
		t.Fatalf("Start events = %+v", events)
	}
	if events[0].SessionID != id {
		t.Fatalf("Event session id %q != returned %q", events[0].SessionID, id)
	}
	return id
}

func mustJoin(t *testing.T, m *Manager, tenant, id, joiner, initiator string) []Event {
	t.Helper()
	events, err := m.Join(tenant, id, joiner)
	if err != nil {
		t.Fatalf("Join(%s): %v", joiner, err)
	}
	if len(events) != 2 {
		t.Fatalf("Join events = %+v", events)
	}
	if events[0].Kind != EvJoined || events[0].Recipient != joiner || events[0].Role != "responder" {
		t.Errorf("join ack = %+v", events[0])
	}
	if events[1].Kind != EvPeerJoined || events[1].Recipient != initiator || events[1].PeerRole != "responder" {
		t.Errorf("peer-joined notice = %+v", events[1])
	}
	return events
}

// ---------------------------------------------------------------- start ---

func TestStartAssignsInitiatorAndCountsPerTenant(t *testing.T) {
	m, _ := newTestManager(2, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	_ = mustStart(t, m, tenantA, connA2)

	// Third session for the same tenant exceeds the cap…
	if _, _, err := m.Start(tenantA, connA3); !errors.Is(err, ErrTooManySessions) {
		t.Fatalf("cap expected ErrTooManySessions, got %v", err)
	}
	// …but a DIFFERENT tenant is unaffected — caps are per tenant by
	// construction, not by a remembered filter.
	_ = mustStart(t, m, tenantB, connB1)

	if sessions, tenants := m.Stats(); sessions != 3 || tenants != 2 {
		t.Fatalf("Stats = (%d, %d)", sessions, tenants)
	}
}

func TestStartRejectsAConnectionAlreadyInASession(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	if _, _, err := m.Start(tenantA, connA1); !errors.Is(err, ErrAlreadyInSession) {
		t.Fatalf("expected ErrAlreadyInSession, got %v", err)
	}
}

// ----------------------------------------------------------------- join ---

func TestJoinSeatsResponderAndWakesInitiator(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)
}

func TestJoinCollapsesUnknownWrongTenantAndEndedIntoOneRefusal(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	for _, tc := range []struct{ tenant, id, conn string }{
		{tenantA, "nope-1", connA2}, // never existed
		{tenantB, id, connB1},       // exists — under another tenant
	} {
		if _, err := m.Join(tc.tenant, tc.id, tc.conn); !errors.Is(err, ErrUnknownSession) {
			t.Errorf("Join(%s, %s) = %v, want ErrUnknownSession", tc.tenant, tc.id, err)
		}
	}

	// Ended sessions vanish the same way — a resurrected capability tells
	// the caller nothing about what used to exist.
	if _, err := m.End(tenantA, id, connA1, ReasonMemberEnded); err != nil {
		t.Fatalf("End: %v", err)
	}
	if _, err := m.Join(tenantA, id, connA2); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("join after end = %v, want ErrUnknownSession", err)
	}
}

func TestJoinRefusesTheCreatorAndAThirdSeat(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	// The initiator's own connection is already bound — joining one's own
	// session must not double-seat it.
	if _, err := m.Join(tenantA, id, connA1); !errors.Is(err, ErrAlreadyInSession) {
		t.Errorf("self-join = %v, want ErrAlreadyInSession", err)
	}

	mustJoin(t, m, tenantA, id, connA2, connA1)
	if _, err := m.Join(tenantA, id, connA3); !errors.Is(err, ErrWrongState) {
		t.Errorf("third seat = %v, want ErrWrongState", err)
	}
}

// ---------------------------------------------------------------- offer ---

const testSDP = "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\n"

func mustOffer(t *testing.T, m *Manager, tenant, id, conn string) {
	t.Helper()
	events, err := m.Offer(tenant, id, conn, testSDP)
	if err != nil {
		t.Fatalf("Offer(%s): %v", conn, err)
	}
	if len(events) != 1 || events[0].Kind != EvForwardOffer || events[0].SDP != testSDP {
		t.Fatalf("offer events = %+v", events)
	}
}

func TestOfferRequiresBothSeats(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	if _, err := m.Offer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrSessionNotReady) {
		t.Fatalf("offer with one member = %v, want ErrSessionNotReady", err)
	}
}

func TestOfferAnswerRenegotiationIsTheOnlyLegalPath(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	mustOffer(t, m, tenantA, id, connA1)

	// Glare: a second offer while one is outstanding is refused — whoever
	// sends it. This is the invariant Perfect Negotiation asks both
	// browsers to maintain; the edge maintains it once, for all of them.
	if _, err := m.Offer(tenantA, id, connA2, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("glare offer = %v, want ErrWrongState", err)
	}
	if _, err := m.Offer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("re-offer while outstanding = %v, want ErrWrongState", err)
	}

	// The offerer cannot answer their own offer; the responder must.
	if _, err := m.Answer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("self-answer = %v, want ErrWrongState", err)
	}
	events, err := m.Answer(tenantA, id, connA2, testSDP)
	if err != nil {
		t.Fatalf("Answer: %v", err)
	}
	if len(events) != 1 || events[0].Kind != EvForwardAnswer || events[0].Recipient != connA1 {
		t.Fatalf("answer events = %+v", events)
	}

	// Renegotiation: from Answered, EITHER member may offer (role flip).
	if _, err := m.Answer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Errorf("double answer = %v, want ErrWrongState", err)
	}
	events, err = m.Offer(tenantA, id, connA2, testSDP)
	if err != nil {
		t.Fatalf("renegotiation offer: %v", err)
	}
	if events[0].Recipient != connA1 {
		t.Errorf("renegotiation offer went to %s, want the original offerer %s", events[0].Recipient, connA1)
	}
}

func TestSignalingMessagesFromNonMembersTellThemNothing(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	// A third connection of the SAME tenant guessing the id learns nothing
	// more than a wrong id would have told it.
	if _, err := m.Offer(tenantA, id, connA3, testSDP); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member offer = %v, want ErrUnknownSession", err)
	}
	if _, err := m.Answer(tenantA, id, connA3, testSDP); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member answer = %v, want ErrUnknownSession", err)
	}
	if _, err := m.Candidate(tenantA, id, connA3, []byte(`{"candidate":"c"}`)); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member candidate = %v, want ErrUnknownSession", err)
	}
	if _, err := m.End(tenantA, id, connA3, ReasonMemberEnded); !errors.Is(err, ErrUnknownSession) {
		t.Errorf("non-member end = %v, want ErrUnknownSession", err)
	}
}

// ------------------------------------------------------------ candidate ---

func TestCandidateRelaysToThePeerOnly(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	raw := []byte(`{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}`)
	events, err := m.Candidate(tenantA, id, connA1, raw)
	if err != nil {
		t.Fatalf("Candidate: %v", err)
	}
	if len(events) != 1 || events[0].Kind != EvForwardCandidate || events[0].Recipient != connA2 {
		t.Fatalf("candidate events = %+v", events)
	}
	if string(events[0].Candidate) != string(raw) {
		t.Errorf("candidate payload rewritten: %s", events[0].Candidate)
	}

	// Trickling the other way is symmetric.
	events, _ = m.Candidate(tenantA, id, connA2, raw)
	if events[0].Recipient != connA1 {
		t.Errorf("reverse candidate went to %s", events[0].Recipient)
	}
}

// ----------------------------------------------------------------- end ---

func TestEndNotifiesBothMembersAndFreesTheTenantSlot(t *testing.T) {
	m, _ := newTestManager(1, time.Minute) // cap of ONE makes release observable
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)

	events, err := m.End(tenantA, id, connA1, ReasonMemberEnded)
	if err != nil {
		t.Fatalf("End: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("end events = %+v", events)
	}
	for _, ev := range events {
		if ev.Kind != EvEnded || ev.Reason != ReasonMemberEnded {
			t.Errorf("end event = %+v", ev)
		}
	}

	// The tenant slot AND the seat binding both came back, in one check:
	// with cap ONE, this Start would fail on the cap if End hadn't freed
	// the tenant counter, and on already-in-session if the conn binding
	// hadn't been released.
	_ = mustStart(t, m, tenantA, connA1)
}

// ------------------------------------------------------------ conn drop ---

func TestConnDroppedEndsTheSessionAndTellsTheSurvivor(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)
	mustJoin(t, m, tenantA, id, connA2, connA1)
	mustOffer(t, m, tenantA, id, connA1)

	events := m.ConnDropped(connA2)
	if len(events) != 1 || events[0].Kind != EvEnded || events[0].Recipient != connA1 || events[0].Reason != ReasonPeerDisconnected {
		t.Fatalf("drop events = %+v", events)
	}
	// The dropper gets no notice (it is gone), and the session is fully
	// gone: the survivor hears NOTHING more, and its seat is reusable.
	if _, _, err := m.Start(tenantA, connA1); err != nil {
		t.Fatalf("survivor restart: %v", err)
	}
}

func TestConnDroppedOnALoneSessionIsSilent(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	if events := m.ConnDropped(connA1); len(events) != 0 {
		t.Fatalf("lone drop events = %+v, want none", events)
	}
	if sessions, _ := m.Stats(); sessions != 0 {
		t.Fatalf("Stats after lone drop = %d sessions", sessions)
	}
}

func TestConnDroppedOnAnUnseatedConnectionIsANoOp(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	_ = mustStart(t, m, tenantA, connA1)
	if events := m.ConnDropped(connA3); len(events) != 0 {
		t.Fatalf("unseated drop = %+v", events)
	}
	if sessions, _ := m.Stats(); sessions != 1 {
		t.Fatalf("session count changed by an unrelated drop: %d", sessions)
	}
}
