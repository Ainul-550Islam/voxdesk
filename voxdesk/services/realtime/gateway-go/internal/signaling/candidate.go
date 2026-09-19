package signaling

import (
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleCandidate relays one ICE candidate. The wire contract keeps the
// candidate as raw JSON and the relay preserves it byte-for-byte — browsers
// and media planes evolve the RTCIceCandidate dictionary independently, and
// an edge that re-serializes fields it half-understands is how connectivity
// bugs get born. The end-of-candidates marker (null, or an empty-string
// candidate) is relayed identically; the peer needs it to stop gathering.
func (r *Router) handleCandidate(sub hub.Subscriber, msg *protocol.ClientMessage) {
	if r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is steered to the media engine; use engine.candidate")
		return
	}
	if !protocol.CandidateIsObject(msg.Candidate) {
		r.refuse(sub, protocol.CodeBadMessage, "candidate must be an ICE candidate object or null")
		return
	}
	events, err := r.m.Candidate(sub.Tenant(), msg.SessionID, sub.Session(), msg.Candidate)
	if err != nil {
		r.fail(sub, err)
		return
	}
	r.emit(events)
}
