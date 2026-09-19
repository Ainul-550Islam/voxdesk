package websocket

import (
	"errors"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// readLoop is the session's inbound driver and runs in the caller of Serve
// (the HTTP handler goroutine). It returns — ending the session — on the
// peer closing, a deadline expiring, a policy close, or the writer tearing
// the socket down.
//
// Deadline policy: before authentication the read deadline is the short
// AuthTimeout (a socket that never says hello must not hold a slot); after
// hello it relaxes to PongTimeout and is thereafter owned by the heartbeat
// (every inbound frame extends it, so a busy session is never reaped).
func (c *Connection) readLoop() {
	c.ws.SetReadLimit(c.cfg.MaxMessageBytes)
	c.ws.SetPongHandler(func(string) error {
		c.touch()
		// gorilla's default ping handler already answers pings with pongs;
		// this hook is where the heartbeat's liveness bookkeeping happens.
		return c.ws.SetReadDeadline(time.Now().Add(c.cfg.PongTimeout))
	})

	_ = c.ws.SetReadDeadline(time.Now().Add(c.cfg.AuthTimeout))
	for {
		messageType, data, err := c.ws.ReadMessage()
		if err != nil {
			return
		}
		c.touch()
		if messageType != gorilla.TextMessage {
			// The protocol is JSON text frames only; a binary frame is a
			// client talking a different protocol, not an attack surface
			// worth decoding.
			c.sendError(protocol.CodeBadMessage, "text frames only")
			continue
		}
		c.metrics.MessageRead()

		if !c.allowMessage() {
			c.metrics.RateLimited()
			c.sendError(protocol.CodeRateLimited, "slow down")
			continue
		}

		msg, err := protocol.DecodeClientMessage(data)
		if err != nil {
			c.sendError(protocol.CodeBadMessage, "unrecognized message shape")
			continue
		}
		if !c.dispatch(msg) {
			return // dispatch closed the session (auth failure, etc.)
		}
	}
}

// dispatch handles one decoded frame. It reports false when the session
// must end.
func (c *Connection) dispatch(msg *protocol.ClientMessage) bool {
	// Pre-auth gate: nothing but hello (and a liveness ping, which is safe
	// to answer — it reveals nothing but uptime, mirroring the internal
	// hub's dispatch) exists until a token has been verified — the public
	// edge's difference from the internal hub is that hello PROVES
	// something here.
	if !c.isAuthenticated() && msg.Type != protocol.TypeHello && msg.Type != protocol.TypePing {
		c.sendError(protocol.CodeHelloRequired, "authenticate first")
		return true
	}

	switch msg.Type {
	case protocol.TypeHello:
		return c.handleHello(msg.Token, msg.WSVersion)
	case protocol.TypeSubscribe:
		c.handleSubscribe(msg.Room)
	case protocol.TypeUnsubscribe:
		c.handleUnsubscribe(msg.Room)
	case protocol.TypePing:
		c.enqueueOwn(protocol.NewPong(time.Now()))
	case protocol.TypeSessionStart, protocol.TypeSessionJoin, protocol.TypeSessionEnd,
		protocol.TypeOffer, protocol.TypeAnswer, protocol.TypeCandidate,
		protocol.TypeEngineOffer, protocol.TypeEngineCandidate, protocol.TypeEnginePublish,
		protocol.TypeEngineSubscribe, protocol.TypeEngineUnsubscribe:
		// Signaling frames share every guard the notice plane has (pre-auth
		// gate above, per-connection frame limiter, same socket lifetime) —
		// they differ only in destination: the session state machine, not
		// the hub's rooms. A nil router (a deployment that never wires one)
		// refuses cleanly rather than panicking the loop.
		if c.signaler == nil {
			c.sendError(protocol.CodeBadMessage, "signaling is not enabled on this gateway")
			return true
		}
		c.signaler.HandleMessage(c, msg)
	default:
		c.sendError(protocol.CodeBadMessage, "unrecognized message type")
	}
	return true
}

// handleHello verifies the token and pins the session to its tenant.
// Reports false when the session must end (any failure: the edge does not
// keep unauthenticated sockets around to try again).
func (c *Connection) handleHello(token string, wsVersion int) bool {
	if c.isAuthenticated() {
		c.sendError(protocol.CodeBadMessage, "already authenticated; reconnect to change identity")
		return true
	}

	claims, err := c.verifier.Verify(token, time.Now())
	if err != nil {
		c.metrics.AuthFailed()
		// Server-side log carries the precise reason; the client gets the
		// generic code, so a forger learns nothing about WHICH check failed.
		c.logf("[gateway] auth failed, session %s: %v", c.sessionID, err)
		c.enqueueOwn(protocol.NewError(protocol.CodeAuthFailed, "invalid or expired token"))
		c.initiateClose(protocol.ClosePolicyViolation, "authentication failed")
		return false
	}

	if err := c.hub.BindTenant(c.sessionID, claims.TenantID); err != nil {
		if errors.Is(err, hub.ErrOverTenantCap) {
			c.sendError(protocol.CodeOverLimit, "too many connections for this account")
			c.initiateClose(protocol.ClosePolicyViolation, "over tenant capacity")
			return false
		}
		c.sendError(protocol.CodeAuthFailed, "session could not be bound")
		c.initiateClose(protocol.ClosePolicyViolation, "bind failed")
		return false
	}

	c.setIdentity(claims)
	if wsVersion >= 2 {
		c.mu.Lock()
		c.steerCapable = true
		c.mu.Unlock()
	}
	c.enqueueOwn(protocol.NewReady(c.sessionID, claims.TenantID, claims.Role, claims.ExpiresAt))
	// Authenticated now: the longer heartbeat-managed deadline applies.
	_ = c.ws.SetReadDeadline(time.Now().Add(c.cfg.PongTimeout))
	return true
}

// handleSubscribe joins (pinned tenant, room) and acknowledges with the
// resulting peer count.
func (c *Connection) handleSubscribe(room string) {
	if !validate.IsRoomName(room) {
		c.sendError(protocol.CodeRoomInvalid, "unknown room; expected calls, metrics, call:<uuid> or campaign:<uuid>")
		return
	}
	if err := c.subscribe(room); err != nil {
		c.sendError(protocol.CodeOverLimit, "too many subscriptions on this connection")
		return
	}
	peers, err := c.hub.Subscribe(c.sessionID, room)
	if err != nil {
		c.unsubscribe(room)
		c.sendError(protocol.CodeBadMessage, "session is no longer registered")
		return
	}
	c.enqueueOwn(protocol.NewSubscribed(room, peers))
}

// handleUnsubscribe leaves (pinned tenant, room); idempotent.
func (c *Connection) handleUnsubscribe(room string) {
	c.hub.Unsubscribe(c.sessionID, room)
	c.unsubscribe(room)
	c.enqueueOwn(protocol.NewUnsubscribed(room))
}

// allowMessage is the per-connection token bucket (internal/ratelimit,
// internally synchronized). It exists for one reason: a hijacked or buggy
// tab must not be able to keep the gateway CPU-busy decoding frames
// unboundedly.
func (c *Connection) allowMessage() bool {
	return c.limiter.Allow()
}
