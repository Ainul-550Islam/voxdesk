package websocket

import (
	"crypto/rand"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/backpressure"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/presence"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/ratelimit"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

// Connection is one live public WebSocket session.
//
// Lifecycle: Upgrade → Serve → (welcome → auth → frames) → close.
// Serve blocks until the session ends and performs all teardown itself, so
// the HTTP handler that called it simply returns.
type Connection struct {
	sessionID string
	ws        *gorilla.Conn

	cfg      config.Config
	hub      *hub.Hub
	verifier *auth.Verifier
	metrics  *metrics.Registry
	signaler *signaling.Router
	presence *presence.Registry
	logf     func(format string, args ...any)

	// outgoing is the bounded queue the hub fans frames into. Buffered; a
	// full queue drops (backpressure.DropNewest) rather than stalling a
	// room — the policy is the queue's, not a select/default at each site.
	outgoing *backpressure.Queue[any]

	// limiter is the per-connection inbound frame budget (see
	// reader.go#allowMessage); internally synchronized, lifetime-owned by
	// the connection, quota sourced from cfg at construction.
	limiter *ratelimit.Limiter

	// mutable session state, guarded by mu: pinned identity after hello.
	mu            sync.Mutex
	tenantID      string
	userID        string
	role          string
	tokenExpiry   time.Time
	subscriptions map[string]struct{}
	// steerCapable records the hello "ws" marker (>=2). Pinned exactly
	// once at hello like tenant identity; read capability-checked by the
	// signaling router through the SteerCapable accessor.
	steerCapable bool

	// lastActivity is unix-nanos of the last inbound frame of ANY kind
	// (data or pong); heartbeat reads it to reap silently-dead peers.
	lastActivity atomic.Int64

	// closeOnce makes termination idempotent across the reader exiting, an
	// administrative RequestClose, and the heartbeat's reaper all firing on
	// the same dead session. closeCode/closeReason are written inside that
	// Once (before `closed` closes) and read by the writer afterwards.
	closeOnce   sync.Once
	closeCode   int
	closeReason string
	closed      chan struct{}
	writerDone  chan struct{}
}

// NewConnection wraps an upgraded socket. The session is NOT registered in
// the hub yet — Serve does that, so a Connection that is created but never
// served cannot leak registry state.
func NewConnection(
	ws *gorilla.Conn,
	cfg config.Config,
	h *hub.Hub,
	verifier *auth.Verifier,
	reg *metrics.Registry,
	signaler *signaling.Router,
	pr *presence.Registry,
) (*Connection, error) {
	sessionID, err := newUUIDv4()
	if err != nil {
		return nil, err
	}
	c := &Connection{
		sessionID:     sessionID,
		ws:            ws,
		cfg:           cfg,
		hub:           h,
		verifier:      verifier,
		metrics:       reg,
		signaler:      signaler,
		presence:      pr,
		logf:          log.Printf,
		outgoing:      backpressure.New[any](cfg.OutgoingBuffer, backpressure.DropNewest),
		subscriptions: make(map[string]struct{}),
		closed:        make(chan struct{}),
		writerDone:    make(chan struct{}),
		// The bucket starts full inside the limiter: a fresh tab
		// reconnecting after a drop must be able to re-assert its
		// subscriptions immediately. cfg's values are range-checked at
		// boot, so MustPolicy cannot panic here.
		limiter: ratelimit.New(ratelimit.MustPolicy(cfg.MessageRatePerSecond, cfg.MessageBurst)),
	}
	c.touch()
	return c, nil
}

// Serve runs the session until it ends, then tears it down completely.
func (c *Connection) Serve() {
	c.metrics.ConnOpened()
	defer c.metrics.ConnClosed()
	defer c.hub.Unregister(c.sessionID)
	// Presence mirrors hub registration for the authenticated USER (the
	// hub tracks sockets; presence tracks the JWT-verified user behind
	// them). Deferred right after hub.Unregister so it runs FIRST under
	// LIFO: the user is unreachable from the hub the moment presence says
	// so, never the reverse.
	defer c.leavePresence()

	// The welcome goes out BEFORE the writer goroutine starts, so this is
	// the only data frame not written by the writer loop — visibly, in one
	// place, with no second writer possible.
	if !c.writeFrameNow(protocol.NewWelcome(c.sessionID, c.cfg.AuthTimeout, c.cfg.PingInterval, time.Now())) {
		return
	}

	c.hub.Register(c)

	// A signaling session must never outlive the socket that seats a
	// member: whichever way this connection ends (peer close, policy close,
	// heartbeat reap, write failure — they all funnel through Serve's
	// return), tell the relay so the surviving peer hears session.ended
	// instead of negotiating with a ghost. Deferred BEFORE hub.Unregister
	// runs (LIFO), so the relay's lookup of the surviving peer still
	// resolves cleanly.
	if c.signaler != nil {
		defer c.signaler.ConnClosed(c)
	}

	go c.writer()
	go c.heartbeat()

	// The reader runs in the caller and blocks for the session's life; it
	// returns on peer close, deadline, policy close, or a torn writer.
	c.readLoop()

	// Whatever ended the reader ends the session: idempotent close, then
	// wait for the writer to drain its final frames and exit so the socket
	// is never closed out from under an in-flight WriteMessage. The wait is
	// backstopped: a peer that has stopped reading must not hold teardown
	// longer than a couple of write deadlines.
	c.initiateClose(protocol.CloseNormal, "session ended")
	select {
	case <-c.writerDone:
	case <-time.After(3*c.cfg.WriteWait + time.Second):
	}
	_ = c.ws.Close()
}

// ---------------------------------------------------------------------------
// hub.Subscriber implementation
// ---------------------------------------------------------------------------

// Session implements hub.Subscriber.
func (c *Connection) Session() string { return c.sessionID }

// Tenant implements hub.Subscriber ("" until hello succeeds).
func (c *Connection) Tenant() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tenantID
}

// Enqueue implements hub.Subscriber: non-blocking; false on a full queue
// (the hub counts that as a backpressure drop; the queue counts it too).
func (c *Connection) Enqueue(msg any) bool {
	return c.outgoing.Enqueue(msg)
}

// RequestClose implements hub.Subscriber (administrative close: shutdown,
// token expiry). Idempotent by construction.
func (c *Connection) RequestClose(code int, reason string) {
	c.initiateClose(code, reason)
}

// ---------------------------------------------------------------------------
// helpers shared by reader/heartbeat/close
// ---------------------------------------------------------------------------

// touch stamps activity (any inbound frame) for the heartbeat reaper.
func (c *Connection) touch() {
	c.lastActivity.Store(time.Now().UnixNano())
}

// lastActivityTime returns the last inbound activity moment.
func (c *Connection) lastActivityTime() time.Time {
	return time.Unix(0, c.lastActivity.Load())
}

// setIdentity pins the verified identity and joins presence; called
// exactly once, at hello. The presence join uses nothing but verified
// claims — the registry never sees a client-asserted identity.
func (c *Connection) setIdentity(claims *auth.Claims) {
	c.mu.Lock()
	c.tenantID = claims.TenantID
	c.userID = claims.UserID
	c.role = claims.Role
	c.tokenExpiry = claims.ExpiresAt
	c.mu.Unlock()
	if c.presence != nil {
		c.presence.Online(claims.TenantID, claims.UserID, c.sessionID)
	}
}

// leavePresence drops this session's presence record. Safe pre-auth:
// without a pinned identity the registry call is a deliberate no-op, and a
// nil registry (a deployment or test that opts out) is tolerated.
func (c *Connection) leavePresence() {
	if c.presence == nil {
		return
	}
	tenantID, userID := c.presenceIdentity()
	if tenantID == "" || userID == "" {
		return
	}
	c.presence.Offline(tenantID, userID, c.sessionID)
}

// identity snapshots the pinned identity (safe for heartbeat/close paths).
func (c *Connection) identity() (tenantID string, role string, expiry time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tenantID, c.role, c.tokenExpiry
}

// presenceIdentity snapshots the (tenant, user) pair presence keys on.
func (c *Connection) presenceIdentity() (tenantID, userID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tenantID, c.userID
}

// SteerCapable implements the optional capability accessor the signaling
// router probes through an interface-assertion on hub.Subscriber: true when
// the client declared wire 1.2 at hello ("ws":2).
func (c *Connection) SteerCapable() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.steerCapable
}

// isAuthenticated reports whether hello has pinned a tenant.
func (c *Connection) isAuthenticated() bool {
	tenantID, _, _ := c.identity()
	return tenantID != ""
}

// subscribe records membership locally (the hub holds the authoritative
// room sets; this mirror exists for the per-connection subscription cap).
func (c *Connection) subscribe(room string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.subscriptions[room]; ok {
		return nil
	}
	if len(c.subscriptions) >= c.cfg.MaxSubscriptionsPerConn {
		return fmt.Errorf("subscription cap %d reached", c.cfg.MaxSubscriptionsPerConn)
	}
	c.subscriptions[room] = struct{}{}
	return nil
}

// unsubscribe drops a local membership mirror.
func (c *Connection) unsubscribe(room string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.subscriptions, room)
}

// newUUIDv4 returns a random RFC 4122 v4 UUID — the same byte shape the
// Python backend, signal-go and the Rust hub all emit, so a session id is
// log-correlatable across every hop.
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
