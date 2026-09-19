package signal

import (
	"context"
	"log"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
)

const (
	idleTimeout = 60 * time.Second
	writeWait   = 10 * time.Second
)

// upgrader is shared across connections; gorilla's Upgrader is safe for
// concurrent use.
var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	// The hub is a backend service reached by app servers and the Next.js
	// dashboard, not by browsers directly; origin checking is left to the
	// edge (the Rust counterpart does no origin filtering either).
	CheckOrigin: func(*http.Request) bool { return true },
}

// Server upgrades WebSocket connections and drives the per-session
// goroutines: exactly one reader and one writer per connection, plus the
// shared hub state guarded by a single mutex.
type Server struct {
	hub  *Hub
	idle time.Duration
	logf func(format string, args ...any)
}

// NewServer returns a Server over hub with the default 60 s idle reaper.
func NewServer(hub *Hub) *Server {
	return &Server{hub: hub, idle: idleTimeout, logf: log.Printf}
}

// ServeHTTP implements http.Handler.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		s.logf("[signal] upgrade failed: %v", err)
		return
	}
	s.serveConn(conn)
}

func (s *Server) serveConn(conn *websocket.Conn) {
	sessionID, err := NewUUIDv4()
	if err != nil {
		_ = conn.Close()
		return
	}
	outgoing := s.hub.register(sessionID)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go s.writeLoop(ctx, conn, outgoing)

	// Welcome is the first frame, before any client traffic is read.
	trySend(outgoing, newWelcome(sessionID))

	s.readLoop(conn, sessionID, outgoing)

	// The reader has returned: the session is over. Tear down the writer and
	// scrub the hub.
	cancel()
	s.hub.removeSession(sessionID)
	_ = conn.Close()
}

// writeLoop forwards buffered outbound frames to the socket. If the socket
// write fails, it closes the connection, which unblocks the reader and ends
// the whole session (mirroring the Rust select-loop break on send error).
func (s *Server) writeLoop(ctx context.Context, conn *websocket.Conn, outgoing <-chan ServerMessage) {
	defer conn.Close()
	for {
		select {
		case msg, ok := <-outgoing:
			if !ok {
				return
			}
			data, err := MarshalServerMessage(msg)
			if err != nil {
				continue
			}
			if err := conn.SetWriteDeadline(time.Now().Add(writeWait)); err != nil {
				return
			}
			if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
				return
			}
			// Outbound traffic counts as activity for the idle reaper, so a
			// busy-but-inbound-quiet session is not torn down.
			_ = conn.SetReadDeadline(time.Now().Add(s.idle))
		case <-ctx.Done():
			return
		}
	}
}

// readLoop reads client frames until the idle deadline expires, the client
// closes, or the writer tears the connection down — any of which reaps the
// session the same way.
func (s *Server) readLoop(conn *websocket.Conn, sessionID string, outgoing chan<- ServerMessage) {
	_ = conn.SetReadDeadline(time.Now().Add(s.idle))
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			return
		}
		_ = conn.SetReadDeadline(time.Now().Add(s.idle))

		msg, err := DecodeClientMessage(data)
		if err != nil {
			trySend(outgoing, newError(CodeBadMessage, "unrecognized message shape"))
			continue
		}
		s.dispatch(sessionID, outgoing, msg)
	}
}

func (s *Server) dispatch(sessionID string, outgoing chan<- ServerMessage, msg *ClientMessage) {
	switch msg.Type {
	case TypeHello:
		if reply, send := s.hub.hello(sessionID, msg.TenantID); send {
			trySend(outgoing, reply)
		}
	case TypeSubscribe:
		if reply, send := s.hub.subscribe(sessionID, msg.Room); send {
			trySend(outgoing, reply)
		}
	case TypeUnsubscribe:
		if reply, send := s.hub.unsubscribe(sessionID, msg.Room); send {
			trySend(outgoing, reply)
		}
	case TypePublish:
		s.hub.publish(sessionID, msg.Room, msg.Payload)
	case TypePing:
		trySend(outgoing, newPong())
	default:
		trySend(outgoing, newError(CodeBadMessage, "unrecognized message type"))
	}
}
