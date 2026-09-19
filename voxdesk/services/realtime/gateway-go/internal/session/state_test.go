package session

import (
	"errors"
	"testing"
	"time"
)

// The state machine as a matrix: for every (state, message) the legal
// outcome is asserted through the manager, not by inspecting internals —
// the machine means nothing except what it lets a pair of clients do.

func TestStateTransitionsMatrix(t *testing.T) {
	m, _ := newTestManager(8, time.Minute)
	id := mustStart(t, m, tenantA, connA1)

	// Pending: offers need two seats; answers are out of phase outright.
	if _, err := m.Offer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrSessionNotReady) {
		t.Fatalf("pending offer = %v", err)
	}
	if _, err := m.Answer(tenantA, id, connA1, testSDP); !errors.Is(err, ErrWrongState) {
		t.Fatalf("pending answer = %v", err)
	}
	mustJoin(t, m, tenantA, id, connA2, connA1)

	// Pending with two seats → offer moves to Offering.
	mustOffer(t, m, tenantA, id, connA1)

	// Offering → answer (from the non-offerer) moves to Answered.
	if _, err := m.Answer(tenantA, id, connA2, testSDP); err != nil {
		t.Fatalf("answer: %v", err)
	}

	// Answered → offer from either side re-enters Offering (renegotiation).
	mustOffer(t, m, tenantA, id, connA2)
	if _, err := m.Answer(tenantA, id, connA1, testSDP); err != nil {
		t.Fatalf("renegotiation answer: %v", err)
	}

	// Any state → end lands in Ended; after end, every signaling message
	// gets the collapsed unknown refusal.
	if _, err := m.End(tenantA, id, connA1, ReasonMemberEnded); err != nil {
		t.Fatalf("End: %v", err)
	}
	for _, tc := range []func() error{
		func() error { _, err := m.Offer(tenantA, id, connA1, testSDP); return err },
		func() error { _, err := m.Answer(tenantA, id, connA2, testSDP); return err },
		func() error { _, err := m.Candidate(tenantA, id, connA1, nil); return err },
		func() error { _, err := m.End(tenantA, id, connA1, ReasonMemberEnded); return err },
	} {
		if err := tc(); !errors.Is(err, ErrUnknownSession) {
			t.Errorf("post-end message = %v, want ErrUnknownSession", err)
		}
	}
}

func TestStateNamesAreStableWireAndLogVocabulary(t *testing.T) {
	// Reasons ride on the wire (session.ended.reason); states serve logs
	// and future dashboards. Pin both vocabularies.
	states := map[State]string{
		Pending:  "pending",
		Offering: "offering",
		Answered: "answered",
		Ended:    "ended",
	}
	for st, want := range states {
		if got := st.String(); got != want {
			t.Errorf("State(%d).String() = %q, want %q", int(st), got, want)
		}
	}
	for _, reason := range []string{ReasonMemberEnded, ReasonPeerDisconnected, ReasonJoinTimeout} {
		if reason == "" || reason != underscoreLower(reason) {
			t.Errorf("reason %q must be a stable snake_case token", reason)
		}
	}
}

func underscoreLower(s string) string {
	for _, r := range s {
		if r != '_' && (r < 'a' || r > 'z') {
			return "?" + s
		}
	}
	return s
}
