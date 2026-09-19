package websocket

import (
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// tokenExpiryGrace is how long a session may outlive its access token's
// exp before the edge closes it. Aligned with auth.clockLeeway (30 s): the
// dashboard rotates its access token on refresh, so a session is only ever
// asked to reconnect — never abruptly cut mid-refresh-cycle — and a
// forgotten tab still dies within a minute of the credential lapsing.
const tokenExpiryGrace = 30 * time.Second

// heartbeat is the session's timekeeper. Every PingInterval it:
//
//  1. sends a WS-level ping (keepalive — proxies and mobile NATs commonly
//     reap idle sockets at 60 s; the browser's automatic pong also proves
//     the peer is still there);
//  2. reaps a peer that has gone silent past PongTimeout (backgrounded
//     tabs lose their timers; dead TCP peers never error politely);
//  3. enforces the access-token expiry: the edge never outlives the
//     credential that opened it, so when the token lapses the socket is
//     closed with a reason the dashboard can reconnect on.
//
// Deadline arithmetic (the subtle part): the read deadline is extended on
// EVERY inbound frame — data or pong — by the reader and pong handler. The
// heartbeat's staleness check is therefore a second, independent tripwire:
// it fires only when NOTHING has arrived for a full PongTimeout, which is
// precisely the case the read deadline would also catch. Two mechanisms,
// one policy: a silently dead peer must not hold a slot.
func (c *Connection) heartbeat() {
	ticker := time.NewTicker(c.cfg.PingInterval)
	defer ticker.Stop()
	for {
		select {
		case <-c.closed:
			return
		case <-ticker.C:
			if !c.pingOnce() {
				return
			}
		}
	}
}

// pingOnce performs one heartbeat round. It reports false when the session
// has ended (so the ticker goroutine exits instead of leaking).
func (c *Connection) pingOnce() bool {
	select {
	case <-c.closed:
		return false
	default:
	}

	// Token-expiry enforcement first: closing for an expired credential is
	// a policy decision, not a liveness failure, and must happen even on a
	// perfectly healthy socket.
	_, _, expiry := c.identity()
	if !expiry.IsZero() && time.Now().After(expiry.Add(tokenExpiryGrace)) {
		c.enqueueOwn(protocol.NewError(protocol.CodeAuthFailed, "access token expired; reconnect with a fresh token"))
		c.initiateClose(protocol.ClosePolicyViolation, "token_expired")
		return false
	}

	// Silent-peer reaper. Pre-auth sessions are not heartbeated: their
	// deadline is the reader's short AuthTimeout, and touching them here
	// would only double the policy's owners.
	if c.isAuthenticated() && time.Since(c.lastActivityTime()) > c.cfg.PongTimeout {
		c.initiateClose(protocol.ClosePolicyViolation, "pong timeout")
		return false
	}

	// WriteControl is safe concurrently with the writer goroutine's data
	// frames (gorilla guarantees it); control frames never queue behind a
	// slow consumer, which is exactly why keepalive belongs here and not in
	// the outgoing channel.
	if err := c.ws.WriteControl(
		gorilla.PingMessage,
		[]byte(c.sessionID),
		time.Now().Add(c.cfg.WriteWait),
	); err != nil {
		c.initiateClose(protocol.CloseGoingAway, "heartbeat write failed")
		return false
	}
	return true
}
