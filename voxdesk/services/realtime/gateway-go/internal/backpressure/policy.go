// Package backpressure owns the one queue policy the gateway has had since
// its first connection — bounded buffer, drop-newest with a counter —
// extracted from the WebSocket connection's outgoing channel so the policy
// is a named, tested type rather than a select/default buried in a loop.
//
// Why drop-newest (and never block): a slow browser must never stall a
// room. A fan-out that BLOCKED on one full queue would let any single
// consumer head-of-line-block every other subscriber of the room — the
// classic slow-consumer amplification that turns one bad connection into a
// room-wide outage. Dropping the frame for the lagging consumer only, and
// COUNTING the drop, is the same contract the Rust hub's BoundedBroadcast
// implements, and the gateway's /metrics has always rendered it as
// voxdesk_gateway_dropped_total.
package backpressure

import "fmt"

// Policy describes how an over-capacity enqueue is resolved.
//
//go:generate stringer -type Policy
type Policy int

const (
	// DropNewest refuses the INCOMING frame when the queue is full: the
	// frames already queued stay queued, the newest arrival is lost, and
	// DroppedTotal increments. For realtime dashboard traffic (where a
	// fresher update always supersedes) this preserves ordering for the
	// frames that do flow and sheds load at the exact point of overload.
	DropNewest Policy = iota
)

// String renders the policy for logs and tests.
func (p Policy) String() string {
	switch p {
	case DropNewest:
		return "drop-newest"
	default:
		return fmt.Sprintf("unknown(%d)", int(p))
	}
}

// Drop semantics for future policies (block-oldest, drop-oldest) belong in
// this package, added alongside their own tests — never as an inline
// select/default at a call site, which is exactly the pattern this package
// replaced.
