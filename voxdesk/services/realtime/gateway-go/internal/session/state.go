// Package session owns the signaling session: a point-to-point negotiation
// between exactly two connections of ONE tenant (the initiator and the
// responder), its state machine, and its per-tenant accounting.
//
// The package is deliberately transport-agnostic: it knows connection IDs
// (strings) and returns Event values describing what happened; it never
// formats frames and never touches sockets. That is what internal/signaling
// is for, and the separation is what keeps the whole state machine
// unit-testable without a single fake websocket.
package session

import "fmt"

// State is the negotiation phase of a session. The vocabulary is the
// smallest set the SERVER can actually know: it can see SDP ordering, so it
// knows Pending/Offering/Answered; it cannot see media flowing, so there is
// intentionally no "Connected" — signaling correctness ends where the
// network gets opaque, and pretending otherwise would codify a guess.
type State int

const (
	// Pending: the session exists, fewer than two members, or no offer yet.
	Pending State = iota
	// Offering: an SDP offer is outstanding; only an answer from the OTHER
	// member may follow. This is the glare guard — two offers outstanding
	// is exactly what Perfect Negotiation exists to avoid, and the server
	// makes it unreachable instead of asking both browsers to be careful.
	Offering
	// Answered: SDP exchange completed. Candidates keep flowing; either
	// member may open a renegotiation with a fresh offer.
	Answered
	// Ended: terminal. An ended session is dropped from the registry in the
	// same critical section that marks it, so Ended is in practice visible
	// only inside the manager's result values.
	Ended
)

// String renders the state for logs and tests.
func (s State) String() string {
	switch s {
	case Pending:
		return "pending"
	case Offering:
		return "offering"
	case Answered:
		return "answered"
	case Ended:
		return "ended"
	default:
		return fmt.Sprintf("state(%d)", int(s))
	}
}
