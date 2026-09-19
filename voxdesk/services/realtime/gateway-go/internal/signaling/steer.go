// Steer-mode signaling: wire-1.2's engine.* frame handlers. Each is the
// counterpart of a classic relay handler but addresses the media engine
// (via engineclient) instead of the peer's socket. The invariants:
//
//  1. A frame is honored ONLY on a session negotiated steered (see
//     negotiateSteer); anything else refuses steer_mode_blocked — mixing
//     media paths per session is forbidden by construction.
//  2. Every engine call is bounded (engineCallTimeout) and NEVER blocks
//     the session's survival: engine failure surfaces as one error frame
//     to the caller + the availability gauge + a classified log line;
//     the session itself remains valid for retry.
//  3. Browser netsession identity never leaks engine-side identities:
//     the wire keys on the GATEWAY session id; the router translates via
//     engineSessions. Fanout to the peer uses protocol frames, never
//     engine frames.
//
// Engine server frames this code understands from one reply: "answer"
// (→ engine.answer to the caller), "track.published" (→ engine.track_
// published to the peer), "error" (→ refuse to the caller). Anything else
// is an engine-side surprise: logged, dropped, counted — never relayed
// blindly (relay-what-you-don't-parse is exactly the trust inversion the
// edge exists to prevent).
package signaling

import (
	"context"
	"encoding/json"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// handleEngineSignal dispatches one engine.* frame under the steered
// invariants (package header).
func (r *Router) handleEngineSignal(sub hub.Subscriber, msg *protocol.ClientMessage) {
	// 1. Mode gate: negotiated steered?
	if !r.IsSteered(msg.SessionID) {
		r.refuse(sub, protocol.CodeSteerModeBlocked, "session is not steered to the media engine (negotiated mode is P2P)")
		return
	}
	if r.eng == nil {
		r.refuse(sub, protocol.CodeEngineUnavailable, "no media engine is configured on this gateway")
		return
	}

	// 2. Membership + translation gate: the caller holds a seat in THE
	//    session it names, and that seat is enrolled engine-side.
	current, ok := r.m.SessionForConn(sub.Session())
	if !ok || current != msg.SessionID {
		r.refuse(sub, protocol.CodeSessionUnknown, "no such session")
		return
	}
	engineSID, ok := r.engineSessions.Load(sub.Session() + ":" + msg.SessionID)
	if !ok {
		// The engine did not admit this member (join hook failed at
		// session time): the frame cannot reach any steerable state —
		// answer the degrade posture rather than pretend.
		r.refuse(sub, protocol.CodeEngineUnavailable, "engine session is not enrolled for this connection")
		return
	}
	sid := engineSID.(string)

	// 3. Per-variant payload guards mirror the relay handlers'.
	ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
	defer cancel()

	switch msg.Type {
	case protocol.TypeEngineOffer:
		sdp := msg.SDP
		if !protocol.IsValidSDP(sdp) {
			r.refuse(sub, protocol.CodeBadMessage, "sdp must be an SDP blob (the 'v=' version line first)")
			return
		}
		if len(sdp) > protocol.MaxSDPLength {
			r.refuse(sub, protocol.CodeSignalTooLarge, "sdp body exceeds the relay cap")
			return
		}
		frames, err := r.eng.Offer(ctx, sid, sdp)
		if err != nil {
			r.engineRefusal(sub, "offer", err)
			return
		}
		r.dispatchEngineFrames(sub, msg.SessionID, frames)

	case protocol.TypeEngineCandidate:
		if !protocol.CandidateIsObject(msg.Candidate) {
			r.refuse(sub, protocol.CodeBadMessage, "candidate must be an ICE candidate object or the null end marker")
			return
		}
		var payload any
		if err := json.Unmarshal(msg.Candidate, &payload); err != nil {
			r.refuse(sub, protocol.CodeBadMessage, "candidate is not parseable JSON")
			return
		}
		frames, err := r.eng.Trickle(ctx, sid, payload)
		if err != nil {
			r.engineRefusal(sub, "trickle", err)
			return
		}
		r.dispatchEngineFrames(sub, msg.SessionID, frames)

	case protocol.TypeEnginePublish:
		frames, err := r.eng.Publish(ctx, sid, msg.Track, msg.Kind)
		if err != nil {
			r.engineRefusal(sub, "publish", err)
			return
		}
		r.dispatchEngineFrames(sub, msg.SessionID, frames)

	case protocol.TypeEngineSubscribe, protocol.TypeEngineUnsubscribe:
		// The peer of a steered 2-member session is THE other member's
		// engine participant; the browser says only WHICH track.
		peerConn, ok := r.m.OtherMember(sub.Tenant(), msg.SessionID, sub.Session())
		if !ok {
			r.refuse(sub, protocol.CodeSessionNotReady, "the peer has not joined yet")
			return
		}
		var err error
		if msg.Type == protocol.TypeEngineSubscribe {
			err = r.eng.Subscribe(ctx, sid, peerConn, msg.Track)
		} else {
			err = r.eng.Unsubscribe(ctx, sid, peerConn, msg.Track)
		}
		if err != nil {
			r.engineRefusal(sub, "subscribe", err)
			return
		}
	}
}

// dispatchEngineFrames turns the engine reply frames into WIRE frames for
// the right sockets (invariant 3). Called synchronously with the engine
// call; any enqueue failure routes a backpressure drop to the metrics the
// same way emit does — a negotiation frame lost to a full peer queue
// desyncs the steered path exactly like the P2P path, so the drop policy
// is shared (see events.go#emit).
func (r *Router) dispatchEngineFrames(caller hub.Subscriber, sessionID string, frames []engineclient.Frame) {
	for _, f := range frames {
		frameType, _ := f["type"].(string)
		switch frameType {
		case "answer":
			sdp, _ := f["sdp"].(string)
			if sdp == "" {
				r.logf("[gateway] steer: engine answer without sdp, session %s", sessionID)
				r.reg.Dropped(1)
				continue
			}
			if !caller.Enqueue(protocol.NewEngineAnswer(sessionID, sdp)) {
				r.reg.Dropped(1)
				r.logf("[gateway] steer: engine answer dropped on full queue, session %s", sessionID)
			}
		case "track.published":
			participant, _ := f["participant"].(string)
			track, _ := f["track"].(string)
			kind, _ := f["kind"].(string)
			if participant == "" || track == "" || kind == "" {
				r.logf("[gateway] steer: engine track.published fields incomplete: %v", f)
				r.reg.Dropped(1)
				continue
			}
			r.fanoutTrackPublished(caller, sessionID, participant, track, kind)
		case "error":
			code, _ := f["code"].(string)
			message, _ := f["message"].(string)
			r.refuse(caller, engineWireCode(code), message)
		default:
			// Never blind-relay an engine-side surprise (invariant header).
			r.logf("[gateway] steer: unexpected engine frame type %q dropped, session %s", frameType, sessionID)
			r.reg.Dropped(1)
		}
	}
}

// fanoutTrackPublished delivers the publish notice to the caller's PEER
// in the session — exactly one socket in the 2-member model.
func (r *Router) fanoutTrackPublished(caller hub.Subscriber, sessionID, participant, track, kind string) {
	peerConn, ok := r.m.OtherMember(caller.Tenant(), sessionID, caller.Session())
	if !ok {
		return // peer left between engine call and dispatch: nobody to tell
	}
	peer, ok := r.lookup(peerConn)
	if !ok {
		return
	}
	if !peer.Enqueue(protocol.NewEngineTrackPublished(sessionID, participant, track, kind)) {
		r.reg.Dropped(1)
		r.logf("[gateway] steer: track.published dropped on full peer queue, session %s", sessionID)
	}
}

// engineRefusal answers an engine-side CALL failure (transport/down/
// structured refusal — all already metered by the client observer) with a
// session-preserving error frame and the classified log the runbook greps.
func (r *Router) engineRefusal(sub hub.Subscriber, op string, err error) {
	if engineclient.IsUnavailable(err) {
		r.logf("[gateway] steer %s UNAVAILABLE participant=%s: %v", op, sub.Session(), err)
		r.refuse(sub, protocol.CodeEngineUnavailable, "media engine is temporarily unavailable; retry in a moment")
		return
	}
	r.logf("[gateway] steer %s REFUSED participant=%s: %v", op, sub.Session(), err)
	r.refuse(sub, protocol.CodeBadMessage, "media engine refused the frame; check the frame shape")
}

// engineWireCode maps the engine's in-band error codes onto the CLOSED
// gateway protocol vocabulary (the browser never sees engine-side code
// names — they are an internal taxonomy, and an unmapped value must not
// leak).
func engineWireCode(code string) string {
	switch code {
	case "bad_message":
		return protocol.CodeBadMessage
	case "room_full", "over_limit":
		return protocol.CodeOverLimit
	case "wrong_state":
		return protocol.CodeWrongSignalState
	default:
		return protocol.CodeBadMessage
	}
}
