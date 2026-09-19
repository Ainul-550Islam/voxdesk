package websocket

import (
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

// writer is the ONLY goroutine that writes data frames to the socket. It
// drains the connection's outgoing queue until the session closes, then
// exits — closing writerDone so Serve can finish teardown without racing a
// final WriteMessage against ws.Close().
//
// Frames are marshalled HERE (not at enqueue time) so the hub never pays
// per-subscriber serialization cost for one publish, and so a frame that
// can never be written (a session already closing) costs nothing.
func (c *Connection) writer() {
	defer close(c.writerDone)
	for {
		select {
		case msg := <-c.outgoing.C():
			if !c.writeFrameNow(msg) {
				// The socket is gone; end the session so the reader and
				// heartbeat stop too (mirrors the internal hub's
				// select-loop break on send error).
				c.initiateClose(protocol.CloseGoingAway, "write failed")
				return
			}
		case <-c.closed:
			// Drain every queued frame (each write is still bounded by
			// WriteWait), THEN send the close frame with the code/reason
			// initiateClose recorded. Data-before-close ordering matters:
			// an auth_failed error that arrives after the close frame is an
			// error the client never sees.
			for {
				select {
				case msg := <-c.outgoing.C():
					if !c.writeFrameNow(msg) {
						return
					}
				default:
					_ = c.ws.WriteControl(
						gorilla.CloseMessage,
						gorilla.FormatCloseMessage(c.closeCode, c.closeReason),
						time.Now().Add(c.cfg.WriteWait),
					)
					return
				}
			}
		}
	}
}

// writeFrameNow marshals and writes one frame with the configured write
// deadline. Reports false when the write failed and the session must end.
//
// Called from (a) Serve before the loops start (the welcome), and (b) the
// writer goroutine — never concurrently, so gorilla's single-writer rule
// holds by construction rather than by a mutex.
func (c *Connection) writeFrameNow(msg any) bool {
	data, err := protocol.Marshal(msg)
	if err != nil {
		// A frame constructed by our own code can only fail to marshal if
		// an ingest payload was not valid JSON — ingest validates that, so
		// this is a can't-happen guard, logged and dropped rather than
		// fatal to the session.
		c.logf("[gateway] marshal failed, session %s: %v", c.sessionID, err)
		return true
	}
	if err := c.ws.SetWriteDeadline(time.Now().Add(c.cfg.WriteWait)); err != nil {
		return false
	}
	if err := c.ws.WriteMessage(gorilla.TextMessage, data); err != nil {
		return false
	}
	return true
}

// enqueueOwn routes the session's OWN replies (ready, subscribed, pong,
// errors) through the same bounded queue as deliveries: one writer, one
// ordering, one backpressure rule. A full queue drops the reply — a session
// that cannot keep up loses non-critical frames before it stalls a room,
// which is exactly the Rust BoundedBroadcast contract.
func (c *Connection) enqueueOwn(msg any) {
	if !c.Enqueue(msg) {
		c.metrics.Dropped(1)
	}
}

// sendError is enqueueOwn specialized for error frames.
func (c *Connection) sendError(code, message string) {
	c.enqueueOwn(protocol.NewError(code, message))
}
