package websocket

import (
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// Close-code policy (a comment instead of constants scattered through the
// lifecycle files, so the WHOLE set is reviewable in one place):
//
//   - protocol.CloseNormal (1000)          — the reader ended; orderly end
//   - protocol.CloseGoingAway (1001)       — server shutdown, failed writer
//   - protocol.ClosePolicyViolation (1008) — auth failed, auth timeout,
//                                            over capacity, token expired,
//                                            pong timeout
//   - protocol.CloseInternalError (1011)   — reserved for unexpected failure
//
// No other code is ever sent. The protocol constants are referenced so the
// compiler enforces they exist; policy comments alone cannot drift.

// initiateClose terminates the session exactly once, racing callers
// notwithstanding: the reader exiting, an administrative RequestClose from
// the hub (shutdown), and the heartbeat's reaper can all fire on the same
// dead session, and only the first one matters.
//
// Ordering contract (why this function does NOT write the close frame
// itself): a session often owes its peer a final DATA frame before the
// close — an auth_failed error, an over-limit notice. The single writer
// goroutine owns all data frames, so termination works like this:
//
//  1. close(c.closed)   — tells the writer to drain queued frames and then
//     write the close frame with the recorded code/reason (writer.go);
//  2. SetReadDeadline in the past — unblocks a reader parked in ReadMessage
//     WITHOUT closing the socket from under the writer's final frames;
//  3. Serve waits for the writer to finish, then closes the socket.
//
// The code/reason are plain fields written here (inside the sync.Once, so
// before `closed` closes) and read by the writer afterwards — the channel
// close provides the happens-before edge, no extra mutex needed.
func (c *Connection) initiateClose(code int, reason string) {
	c.closeOnce.Do(func() {
		c.closeCode = code
		c.closeReason = reason
		close(c.closed)
		_ = c.ws.SetReadDeadline(time.Now().Add(-time.Second))
		if code != protocol.CloseNormal {
			c.logf("[gateway] closing session %s: code %d reason %q",
				c.sessionID, code, reason)
		}
	})
}
