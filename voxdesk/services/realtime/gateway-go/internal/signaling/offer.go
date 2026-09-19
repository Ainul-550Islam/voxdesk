package signaling

import (
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleOffer relays one SDP offer to the peer. The guards here are exactly
// the ones the relay can honestly make — payload envelope (starts like SDP,
// fits the cap) and phase legality (one outstanding at a time, which the
// session manager enforces). Whether the SDP describes a media session the
// peer can accept is the peer's business: the gateway's two irrelevant
// skills are evaluating codecs and being lied to.
func (r *Router) handleOffer(sub hub.Subscriber, msg *protocol.ClientMessage) {
	// Validity is judged on the trimmed view; the FORWARDED body is the
	// wire bytes untouched — trailing CRLF is how every SDP line ends, and
	// an edge that trims is an edge that rewrites.
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
	events, err := r.m.Offer(sub.Tenant(), msg.SessionID, sub.Session(), sdp)
	if err != nil {
		r.fail(sub, err)
		return
	}
	r.emit(events)
}
