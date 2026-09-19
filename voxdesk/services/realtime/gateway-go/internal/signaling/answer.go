package signaling

import (
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleAnswer relays one SDP answer to the offerer. Envelope guards are
// identical to the offer path; the interesting legality (there IS an
// outstanding offer and the answerer is not the offerer — otherwise a
// browser could "answer" its own glare away and desync the pair) lives in
// the session state machine, which is why this file stays small. Small is
// the review surface you want on a security edge.
func (r *Router) handleAnswer(sub hub.Subscriber, msg *protocol.ClientMessage) {
	// Validity on the trimmed view, forwarding on the wire bytes (see
	// offer.go: a relay that trims is a relay that rewrites).
	if r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is steered to the media engine; use engine.offer")
		return
	}
	sdp := msg.SDP
	if !protocol.IsValidSDP(sdp) {
		r.refuse(sub, protocol.CodeBadMessage, "sdp must be an SDP blob (the 'v=' version line first)")
		return
	}
	if len(sdp) > protocol.MaxSDPLength {
		r.refuse(sub, protocol.CodeSignalTooLarge, "sdp body exceeds the relay cap")
		return
	}
	events, err := r.m.Answer(sub.Tenant(), msg.SessionID, sub.Session(), sdp)
	if err != nil {
		r.fail(sub, err)
		return
	}
	r.emit(events)
}
