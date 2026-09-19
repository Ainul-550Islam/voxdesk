package signaling

import (
	"context"
	"errors"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
)

// engineCallTimeout bounds ONE engine control hop. The engine client's own
// backstop may be configured longer; hook calls must never stall the caller
// connection's read loop beyond this — engine slowness is a degrade signal,
// not a session-establishment blocker.
const engineCallTimeout = 1200 * time.Millisecond

// This file turns session-layer outcomes into wire frames. Two directions:
// event → frame (emit), error → refusal (fail). Keeping both in one place
// is what guarantees the two vocabularies — the session layer's reasons and
// the protocol's codes — never drift: any new EventKind or sentinel error
// has to pass review HERE, next to its sibling translations.

// emit delivers each event to its addressed connection. A recipient that
// already vanished (it closed between the manager's decision and this
// lookup) is skipped silently: ConnClosed handles session-ending on socket
// death, so nothing is lost by the skip — the frame's recipient no longer
// exists to receive anything.
// emit is the standard-entry shim for paths that carry no steer
// information (everything except session.join, which negotiates the media
// path). Kept arity-stable for existing call sites.
func (r *Router) emit(events []session.Event) {
	r.emitWithSteer(events, nil)
}

// emitWithSteer delivers events; steerByID (built once per session.join —
// see router.go#negotiateSteer) translates EvJoined/EvPeerJoined into the
// negotiated-mode variants of their ack frames. A session absent from the
// map reports steer=false on wire 1.2 semantics (`{}` for the pre-1.2
// paths — same value, no information loss).
func (r *Router) emitWithSteer(events []session.Event, steerByID map[string]bool) {
	// Session-open and session-close are ONCE-PER-SESSION facts; a 2-member
	// end produces two EvEnded events (one per member), but the gauge must
	// move exactly once.
	endedSeen := make(map[string]struct{}, len(events))
	steerCleared := make(map[string]struct{}, len(events))
	for _, ev := range events {
		// Engine leave is NOT subject to the recipient's socket still
		// existing: an EvEnded for a just-closed connection is precisely
		// the close whose engine session must be scrapped, and the
		// lookup-guarded path below would skip it. Idempotent (LoadAnd
		// Delete on a missing key is a no-op). Steer registry entries are
		// per-session: cleared once, on the FIRST end event, via a
		// dedicated set so the once-per-session GAUGE below keeps its own
		// independence (the two once-gates track different facts).
		if ev.Kind == session.EvEnded {
			r.engineLeaveHook(ev.Recipient, ev.SessionID)
			if _, seen := steerCleared[ev.SessionID]; !seen {
				steerCleared[ev.SessionID] = struct{}{}
				r.steerSessions.Delete(ev.SessionID)
			}
		}
		sub, ok := r.lookup(ev.Recipient)
		if !ok {
			continue
		}
		var frame any
		relayed := false
		switch ev.Kind {
		case session.EvStarted:
			frame = protocol.NewSessionStarted(ev.SessionID)
			r.reg.SignalSessionOpened()
			r.engineJoinHook(sub, ev.SessionID)
		case session.EvJoined:
			frame = protocol.NewSessionJoined(ev.SessionID, steerByID[ev.SessionID])
			r.engineJoinHook(sub, ev.SessionID)
		case session.EvPeerJoined:
			frame = protocol.NewSessionPeerJoined(ev.SessionID, ev.PeerRole, steerByID[ev.SessionID])
		case session.EvEnded:
			frame = protocol.NewSessionEnded(ev.SessionID, ev.Reason)
			if _, seen := endedSeen[ev.SessionID]; !seen {
				endedSeen[ev.SessionID] = struct{}{}
				r.reg.SignalSessionClosed()
			}
		case session.EvForwardOffer:
			frame = protocol.NewSignalOffer(ev.SessionID, ev.SDP)
			relayed = true
		case session.EvForwardAnswer:
			frame = protocol.NewSignalAnswer(ev.SessionID, ev.SDP)
			relayed = true
		case session.EvForwardCandidate:
			frame = protocol.NewSignalCandidate(ev.SessionID, ev.Candidate)
			relayed = true
		default:
			// A new EventKind without a translation is a build-time bug;
			// NOTICE it in the log rather than fanning out mystery frames.
			r.logf("[gateway] signaling: unhandled event kind %d for session %s", ev.Kind, ev.SessionID)
			continue
		}
		if !sub.Enqueue(frame) {
			// Backpressure-drop on a NEGOTIATION is different from dropping
			// a notice: a missing offer silently desyncs the pair. End the
			// session so both sides start clean instead of half-connected.
			r.reg.Dropped(1)
			r.logf("[gateway] signaling: drop on full queue, ending session %s", ev.SessionID)
			_, _ = r.m.End(sub.Tenant(), ev.SessionID, ev.Recipient, session.ReasonPeerDisconnected)
			continue
		}
		if relayed {
			r.relayedTotal.Add(1)
			r.reg.SignalRelayed()
		}
	}
}

// refuse renders a protocol-level rejection (bad payload, oversized body),
// for checks that happen BEFORE the session layer is consulted and so have
// no sentinel error to translate.
func (r *Router) refuse(sub hub.Subscriber, code, message string) {
	sub.Enqueue(protocol.NewError(code, message))
}

// fail renders one error frame to the sender. The session layer's sentinel
// errors map onto the closed protocol vocabulary here and nowhere else, so
// "which client sees which reason" is auditable in one switch.
func (r *Router) fail(sub hub.Subscriber, cause error) {
	var code, message string
	switch {
	case errors.Is(cause, session.ErrUnknownSession):
		// Collapsed by design (unknown id, wrong tenant, or non-member):
		// the client learns "not a session you can name", never WHICH fact
		// failed — the distribution of live sessions is tenant data.
		code, message = protocol.CodeSessionUnknown, "no such session"
	case errors.Is(cause, session.ErrTooManySessions):
		code, message = protocol.CodeSessionFull, "too many concurrent sessions for this account"
	case errors.Is(cause, session.ErrAlreadyInSession):
		code, message = protocol.CodeAlreadyInSession, "this connection already holds a session"
	case errors.Is(cause, session.ErrSessionNotReady):
		code, message = protocol.CodeSessionNotReady, "the peer has not joined yet"
	case errors.Is(cause, session.ErrWrongState), errors.Is(cause, session.ErrSessionFull):
		code, message = protocol.CodeWrongSignalState, "not possible in the session's current state"
	default:
		code, message = protocol.CodeBadMessage, "could not process the signaling message"
	}
	sub.Enqueue(protocol.NewError(code, message))
}

// ---- media-engine lifecycle hooks ----------------------------------------
//
// Boundary (matches the package contract): the gateway's signaling session
// is the AUTHORITY over who may hold engine media sessions. Every joined
// member gets exactly one engine join (participant = their connection id —
// never self-asserted identity); every end — graceful, dropped-socket,
// reaper, or double — produces a leave for THAT member's engine session.
// Ordering is linear because these hooks fire inside emit, which the
// session manager's lock already serializes per session.
//
// Failure policy: an unreachable or refusing engine NEVER breaks session
// establishment (graceful degradation is the product requirement; the
// media plane is additive from the client's perspective). Failures surface
// exactly three ways: engineclient's observer metric, the availability
// gauge (the monitor owns it), and this structured log line — all three
// correlation-friendly, none containing ICE credentials.

// engineJoinHook enrolls one joining member. Called under emit, INLINE and
// bounded by engineCallTimeout: the worst case is one slow read-loop tick
// on the JOINING connection, which is the degradation trade accepted for
// a guarantee that leave events (emit-serialized after join) never race a
// still-in-flight join goroutine for the same member.
func (r *Router) engineJoinHook(sub hub.Subscriber, sessionID string) {
	if r.eng == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
	defer cancel()
	room := sub.Tenant() + ":" + sessionID
	res, err := r.eng.Join(ctx, room, sub.Session())
	key := sub.Session() + ":" + sessionID
	if err != nil {
		// Unavailable faults were already metered; the LOG names the class
		// so on-call can tell "engine box down" from "protocol skew" at a
		// glance without unwrapping error text.
		if engineclient.IsUnavailable(err) {
			r.logf("[gateway] engine join UNAVAILABLE room=%s session=%s participant=%s: %v", room, sessionID, sub.Session(), err)
		} else {
			r.logf("[gateway] engine join REFUSED room=%s session=%s participant=%s: %v", room, sessionID, sub.Session(), err)
		}
		return
	}
	r.engineSessions.Store(key, res.Session)
}

// engineLeaveHook releases one member's engine session. Unknown/duplicate
// ends LoadAndDelete a missing entry and return silently — the engine-side
// teardown is idempotent, so the replay path cannot double-free.
func (r *Router) engineLeaveHook(connID, sessionID string) {
	if r.eng == nil {
		return
	}
	key := connID + ":" + sessionID
	v, ok := r.engineSessions.LoadAndDelete(key)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
	defer cancel()
	if err := r.eng.Leave(ctx, v.(string)); err != nil {
		// The engine's session sweep is the safety net for a lost leave
		// (lease expiry on its side); the log line keeps the occurrence
		// attributable to a session id.
		r.logf("[gateway] engine leave failed participant=%s session=%s engine_session=%s: %v", connID, sessionID, v.(string), err)
	}
}

// engineLeaveAllForConn closes every engine session id recorded under one
// connection id: the ConnClosed safety net (see ConnClosed's comment).
// Called with no lock — engineSessions is a sync.Map; the keys are
// connID+":"+sessionID so a prefix scan is exact, never heuristic.
func (r *Router) engineLeaveAllForConn(connID string) {
	if r.eng == nil {
		return
	}
	prefix := connID + ":"
	var keys []string
	r.engineSessions.Range(func(k, _ any) bool {
		if ks, ok := k.(string); ok && len(ks) > len(prefix) && ks[:len(prefix)] == prefix {
			keys = append(keys, ks)
		}
		return true
	})
	for _, key := range keys {
		v, ok := r.engineSessions.LoadAndDelete(key)
		if !ok {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), engineCallTimeout)
		if err := r.eng.Leave(ctx, v.(string)); err != nil {
			r.logf("[gateway] engine leave failed participant=%s (conn closed) engine_session=%s: %v", connID, v.(string), err)
		}
		cancel()
	}
}
