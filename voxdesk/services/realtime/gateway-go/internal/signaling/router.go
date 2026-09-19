// Package signaling is the relay between two authenticated connections that
// share one signaling session: it validates the wire envelope (sizes,
// shapes, phases), drives the session state machine, and translates its
// events into protocol frames enqueued on the right sockets.
//
// Boundaries, stated precisely:
//
//   - It is NOT the realtime notice plane. Rooms, subscriptions, and ingest
//     deliveries belong to the hub; a signaling frame never touches them.
//   - It is NOT auth. The caller (internal/websocket) only dispatches here
//     AFTER hello — Subscriber.Tenant() is by then a verified claim, and
//     this package treats "" as a bug, not input.
//   - It is NOT a media authority. SDP and ICE payloads are forwarded
//     byte-for-byte inside one session; the edge checks envelopes, never
//     contents, because it has no ground truth to check contents against.
package signaling

import (
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
)

// LookupFunc resolves a connection id to its live socket. Provided by the
// hub at wiring time so this package never locks the hub itself — same
// interface-seam as the fan-out path.
type LookupFunc func(connID string) (hub.Subscriber, bool)

// Router is the signaling front: message handlers called from the
// connection read loop, plus the transport-facing close hook.
type Router struct {
	m      *session.Manager
	lookup LookupFunc
	reg    *metrics.Registry
	logf   func(format string, args ...any)

	// relayedTotal frames forwarded across ALL sessions since boot. Held
	// here (not the metrics registry) because it is a signal-plane concern
	// exported through the gateway's /metrics all the same.
	relayedTotal atomic.Int64

	// eng is the Rust media-engine control client; nil = engine plane
	// disabled (dev), which the hooks treat as "nothing to signal" — NOT
	// as a failure. engineSessions records conn→engine media-session ids
	// so leaves reach the engine scoped to the SAME session its join
	// minted (idempotency: replayed ends call Leave on a session the
	// engine already teared down — the engine answers quiet success).
	eng            *engineclient.Client
	engineSessions sync.Map // key: connID+":"+sessionID, value: engine session id

	// Steer plane (wire 1.2): steerMode is the config's closed vocabulary
	// ("off"|"v1.2"|"force"); steerSessions records the negotiated mode
	// per GATEWAY session id (true = media runs through the engine).
	// Router-owned rather than session.Session-attached: the mode is a
	// gateway-engine wire concern, while the session package stays
	// media-agnostic by charter.
	steerMode     string
	steerSessions sync.Map // key: gateway session id, presence = steered
}

// SetEngine attaches the engine control client post-construction (same
// contract as Server.SetEngine: existing New call sites do not reshape).
func (r *Router) SetEngine(eng *engineclient.Client) { r.eng = eng }

// SetSteerMode pins the closed-vocabulary mode from configuration. Called
// together with SetEngine by the wiring layer (server.SetEngine) — no
// additional NewRouter arity. Values never validated here; config.Load
// owns the closed set.
func (r *Router) SetSteerMode(mode string) { r.steerMode = mode }

// NewRouter wires the relay to a session manager and a connection lookup.
func NewRouter(m *session.Manager, lookup LookupFunc, reg *metrics.Registry) *Router {
	return &Router{m: m, lookup: lookup, reg: reg, logf: log.Printf}
}

// Manager exposes the session manager (the reaper loop and /metrics read
// their state through it).
func (r *Router) Manager() *session.Manager { return r.m }

// RelayedTotal is the forwarded-frame counter (offers + answers +
// candidates delivered to a peer's queue).
func (r *Router) RelayedTotal() int64 { return r.relayedTotal.Load() }

// HandleMessage routes one decoded signaling frame. The caller guarantees
// the sender is authenticated; the router guarantees the receiver, if any,
// is the session's OTHER member and nobody else — that is the entire
// routing contract.
func (r *Router) HandleMessage(sub hub.Subscriber, msg *protocol.ClientMessage) {
	switch msg.Type {
	case protocol.TypeSessionStart:
		tenant := sub.Tenant()
		if tenant == "" {
			// Defense in depth: the read loop's pre-auth gate makes this
			// unreachable today; if a future caller ever dispatches here
			// earlier, signaling still refuses an unpinned tenant.
			sub.Enqueue(protocol.NewError(protocol.CodeHelloRequired, "authenticate first"))
			return
		}
		_, events, err := r.m.Start(tenant, sub.Session())
		if err != nil {
			r.fail(sub, err)
			return
		}
		r.emit(events)

	case protocol.TypeSessionJoin:
		events, err := r.m.Join(sub.Tenant(), msg.SessionID, sub.Session())
		if err != nil {
			r.fail(sub, err)
			return
		}
		r.emitWithSteer(events, r.negotiateSteer(sub, msg.SessionID, events))

	case protocol.TypeSessionEnd:
		events, err := r.m.End(sub.Tenant(), msg.SessionID, sub.Session(), session.ReasonMemberEnded)
		if err != nil {
			r.fail(sub, err)
			return
		}
		r.emit(events)

	case protocol.TypeOffer:
		r.handleOffer(sub, msg)
	case protocol.TypeAnswer:
		r.handleAnswer(sub, msg)
	case protocol.TypeCandidate:
		r.handleCandidate(sub, msg)
	case protocol.TypeEngineOffer, protocol.TypeEngineCandidate,
		protocol.TypeEnginePublish, protocol.TypeEngineSubscribe, protocol.TypeEngineUnsubscribe:
		r.handleEngineSignal(sub, msg)
	default:
		r.refuse(sub, protocol.CodeBadMessage, "unrecognized signaling message type")
	}
}

// ---- steer plane -----------------------------------------------------------

// negotiateSteer computes the session's media-mode decision AT the moment
// the second member joins (the only point where both members' capabilities
// are knowable), records it, and returns it for the ack frames.
//
// Predicate, stated once (all terms must hold):
//
//	mode != "off" AND engine link up AND (mode == "force" OR BOTH members
//	sent the v1.2 marker at hello)
//
// force does NOT override an unavailable engine: joining a steered session
// against a dead SFU hangs every call at the engine's own admission gate —
// landing both members on the P2P relay instead is the graceful-degrade
// posture the platform requires, with the fallback visible in this log+
// metric.
func (r *Router) negotiateSteer(joined hub.Subscriber, sessionID string, events []session.Event) map[string]bool {
	steer := false
	defer func() {
		if steer {
			r.steerSessions.Store(sessionID, true)
		}
	}()
	if r.eng == nil || r.steerMode == "" || r.steerMode == "off" || !r.eng.Up() {
		return map[string]bool{sessionID: false}
	}
	if r.steerMode == "force" {
		steer = true
		return map[string]bool{sessionID: true}
	}
	// v1.2: both members must be steer-capable.
	if !steerCapable(joined) {
		return map[string]bool{sessionID: false}
	}
	// The initiator's connection id is on the EvPeerJoined event.
	var initiatorID string
	for _, ev := range events {
		if ev.Kind == session.EvPeerJoined {
			initiatorID = ev.Recipient
			break
		}
	}
	if initiatorID == "" {
		return map[string]bool{sessionID: false}
	}
	initiator, ok := r.lookup(initiatorID)
	if !ok || !steerCapable(initiator) {
		return map[string]bool{sessionID: false}
	}
	steer = true
	return map[string]bool{sessionID: true}
}

// steerCapable probes the connection through the optional accessor without
// the hub.Subscriber interface growing (contract: it stays four methods).
func steerCapable(sub hub.Subscriber) bool {
	type marker interface{ SteerCapable() bool }
	if m, ok := sub.(marker); ok {
		return m.SteerCapable()
	}
	return false
}

// IsSteered reports the negotiated mode of ONE session — used by the P2P
// handlers (refuse-mixing rule) and by tests.
func (r *Router) IsSteered(sessionID string) bool {
	_, steered := r.steerSessions.Load(sessionID)
	return steered
}

// ConnClosed ends whatever session the connection held a seat in and tells
// the surviving peer. Called from the websocket teardown path — which is
// reached for EVERY close shape (peer close, policy close, write failure,
// heartbeat reap) — so a session can never outlive the sockets it binds.
func (r *Router) ConnClosed(sub hub.Subscriber) {
	// A pending (never-joined) session's drop is silent to the peer set —
	// there IS no peer — but its engine session is real and must not
	// leak: close every engine session this conn ever minted HERE, then
	// let EvEnded leaves (idempotent, LoadAndDelete) handle members of
	// joined sessions through the normal emit path.
	r.engineLeaveAllForConn(sub.Session())
	r.emit(r.m.ConnDropped(sub.Session()))
}

// StartReaper runs the lifecycle sweep until the returned stop function is
// called. Owned here rather than in the manager because reaping produces
// NOTIFICATIONS, and frames are this package's concern.
func (r *Router) StartReaper() (stop func()) {
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(r.m.ReapInterval())
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				r.emit(r.m.Reap())
			case <-done:
				return
			}
		}
	}()
	return func() { close(done) }
}
