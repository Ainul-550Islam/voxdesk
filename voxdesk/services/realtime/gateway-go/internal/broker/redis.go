package broker

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// RedisBroker is the multi-replica transport: MemoryBroker's synchronous
// local delivery PLUS a Redis pub/sub hop that carries every publish to
// the other gateway replicas, each of which delivers to its OWN local
// subscribers through the identical code path.
//
// The RESP client here is hand-rolled by design — the gateway's one
// dependency today is gorilla/websocket, and redis pub/sub needs exactly
// five commands (AUTH, SELECT, SUBSCRIBE, PUBLISH, QUIT) and three RESP
// types (simple strings, integers, bulk strings inside arrays). A full
// go-redis dependency to speak five commands is not a trade this edge
// makes (same reasoning as the hand-rolled metrics exposition).
//
// Connection topology: TWO TCP connections. redis forces a connection into
// subscriber-only mode once it SUBSCRIBEs, so the subscriber and publisher
// cannot share one; and PUBLISH replies must be read (else server replies
// eventually back-pressure the socket), so PUBLISH is a synchronous
// write+read under a mutex rather than fire-and-forget.
//
// Resilience: a supervisor goroutine owns connects and reconnects with
// exponential backoff forever (until Close). While the bus is down,
// Publish still delivers locally and reports its local Stats — ingest
// stays a 200 with exact THIS-node counters; cross-node propagation simply
// resumes on reconnect (realtime events five seconds late are worthless
// anyway, so no spool: dropped-on-outage is the deliberate semantic, and
// it is counted by BusDrops).
type RedisBroker struct {
	local  *MemoryBroker
	params redisParams

	logf func(format string, args ...any) // warnings only; nil-safe

	mu      sync.Mutex          // guards pub/sub conns + generation
	pub     *redisConn          // nil while (re)connecting
	sub     *redisConn          // nil while (re)connecting
	topics  map[string]struct{} // union of Subscribe()d topics, applied on every connect
	closed  atomic.Bool
	done    chan struct{} // closed by Close: stops the supervisor
	readyCh chan struct{} // closed on FIRST successful connect (tests/ops)

	busDrops     atomic.Int64 // publishes with no live bus (outage)
	busErrors    atomic.Int64 // failed bus operations
	connected    atomic.Bool
	connectCount atomic.Int64
	readyOnce    sync.Once // guards the one-shot readyCh close
}

// redisParams is everything the dialer needs, parsed from the URL once at
// construction (config.Load already shape-checked the scheme).
type redisParams struct {
	addr     string // host:port
	user     string // ACL user ("" = default)
	password string
	db       int
}

// RedisOption customizes a RedisBroker at construction.
type RedisOption func(*RedisBroker)

// WithRedisLogger installs the warning logger (bus down, reconnecting,
// protocol surprises). Production passes an observability.Logger's Warnf.
func WithRedisLogger(logf func(format string, args ...any)) RedisOption {
	return func(b *RedisBroker) { b.logf = logf }
}

// NewRedis parses redisURL (redis://[user:pass@]host[:port][/db]) and
// starts the connect supervisor. Connection is ASYNCHRONOUS: boot never
// blocks on a reachable bus (a gateway that refuses to serve because an
// optional cross-node hop is down would turn a partial degradation into a
// full outage). An unparsable URL IS a constructor error — that one is a
// deploy-time misconfiguration, exactly what boot-time refusal exists for.
func NewRedis(redisURL string, opts ...RedisOption) (*RedisBroker, error) {
	params, err := parseRedisURL(redisURL)
	if err != nil {
		return nil, err
	}
	b := &RedisBroker{
		local:   NewMemory(),
		params:  params,
		topics:  make(map[string]struct{}),
		done:    make(chan struct{}),
		readyCh: make(chan struct{}),
	}
	for _, opt := range opts {
		opt(b)
	}
	go b.supervise()
	return b, nil
}

// parseRedisURL extracts dial parameters; it validates hard (this runs at
// boot, where a typo must be loud).
func parseRedisURL(raw string) (redisParams, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return redisParams{}, fmt.Errorf("broker: redis URL unparsable: %w", err)
	}
	if u.Scheme != "redis" {
		return redisParams{}, fmt.Errorf("broker: redis URL must use the redis:// scheme, got %q", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return redisParams{}, errors.New("broker: redis URL needs a host")
	}
	port := u.Port()
	if port == "" {
		port = "6379"
	}
	p := redisParams{addr: net.JoinHostPort(host, port)}
	if u.User != nil {
		p.user = u.User.Username()
		p.password, _ = u.User.Password()
	}
	// Path is /db for a single-digit-or-two db number; anything else (a
	// keyspace path, nested path) is a misconfiguration, not a db.
	switch path := strings.TrimPrefix(u.Path, "/"); {
	case path == "":
		// db 0
	case strings.Contains(path, "/"):
		return redisParams{}, fmt.Errorf("broker: redis URL db must be a bare number, got path %q", u.Path)
	default:
		db, err := strconv.Atoi(path)
		if err != nil || db < 0 || db > 15 {
			return redisParams{}, fmt.Errorf("broker: redis URL db must be 0..15, got %q", path)
		}
		p.db = db
	}
	return p, nil
}

// Kind implements Broker.
func (b *RedisBroker) Kind() string { return "redis" }

// NodeID implements Broker.
func (b *RedisBroker) NodeID() string { return b.local.NodeID() }

// Subscribe implements Broker: registers locally (deliveries come through
// the local table in every case) and records the topic so every connect
// round SUBSCRIBEs it on the bus.
func (b *RedisBroker) Subscribe(topic string, h Handler) (unsubscribe func()) {
	b.mu.Lock()
	b.topics[topic] = struct{}{}
	sub := b.sub
	b.mu.Unlock()
	if sub != nil {
		// Live connection: subscribe NOW (best effort; the reconnect loop
		// re-applies the full topic set anyway if this socket dies).
		_ = sub.writeCommand("SUBSCRIBE", topic)
	}
	return b.local.Subscribe(topic, h)
}

// Publish implements Broker: local synchronous delivery FIRST (the
// ingest-response contract), then a best-effort bus hop. Bus failures
// never fail the publish — the returned Stats is the local accounting,
// the error is only ErrClosed.
func (b *RedisBroker) Publish(topic string, payload []byte) (Stats, error) {
	if b.closed.Load() {
		return Stats{}, ErrClosed
	}
	stats := b.local.deliver(Envelope{
		Topic:   topic,
		Payload: json.RawMessage(payload),
		Origin:  b.local.NodeID(),
		Local:   true,
		At:      time.Now(),
	})
	if err := b.publishBus(topic, payload); err != nil {
		if !errors.Is(err, errBusDown) {
			b.busErrors.Add(1)
		} else {
			b.busDrops.Add(1)
		}
		b.warnf("bus publish failed (local delivery unaffected), topic %s: %v", topic, err)
	}
	return stats, nil
}

// wireMessage is the bus frame: origin for own-echo suppression, payload
// base64'd so the JSON wrapper tolerates arbitrary bytes.
type wireMessage struct {
	Origin  string `json:"o"`
	Payload string `json:"p"` // base64 std
	At      int64  `json:"t"` // unix nanos, informational
}

// errBusDown marks "no live connection" publishes: counted separately from
// protocol errors because an outage is expected behaviour, not corruption.
var errBusDown = errors.New("broker: bus connection down")

// publishBus encodes and ships one message. Synchronous write+reply-read
// under the publish mutex: unread :integer replies would otherwise pile up
// in kernel buffers on a quiet topic until the socket stalls.
func (b *RedisBroker) publishBus(topic string, payload []byte) error {
	frame, err := json.Marshal(wireMessage{
		Origin:  b.local.NodeID(),
		Payload: base64.StdEncoding.EncodeToString(payload),
		At:      time.Now().UnixNano(),
	})
	if err != nil {
		return err
	}
	b.mu.Lock()
	pub := b.pub
	b.mu.Unlock()
	if pub == nil {
		return errBusDown
	}
	return pub.publish(topic, string(frame))
}

// Close implements Broker: idempotent, stops the supervisor and closes
// both sockets (which also unblocks their reader goroutines).
func (b *RedisBroker) Close() error {
	if !b.closed.CompareAndSwap(false, true) {
		return nil
	}
	close(b.done)
	b.mu.Lock()
	b.closeConnsLocked()
	b.mu.Unlock()
	return b.local.Close()
}

// Ready reports (and non-blockingly probes) the first successful connect;
// ReadyChan exposes it for tests and future readiness wiring.
func (b *RedisBroker) ReadyChan() <-chan struct{} { return b.readyCh }

// Connected reports whether the bus is currently dialed (diagnostics).
func (b *RedisBroker) Connected() bool { return b.connected.Load() }

// BusDrops / BusErrors expose outage accounting (the /metrics surface for
// cross-node health once a deployment enables redis mode).
func (b *RedisBroker) BusDrops() int64  { return b.busDrops.Load() }
func (b *RedisBroker) BusErrors() int64 { return b.busErrors.Load() }

// ConnectCount is how many connect rounds have succeeded since boot.
func (b *RedisBroker) ConnectCount() int64 { return b.connectCount.Load() }

// supervise is the connect/reconnect loop: dial both connections, run the
// subscriber reader, and on ANY failure tear everything down, back off
// (250 ms doubling to a 5 s ceiling), and try again — until Close. One
// goroutine owns the whole lifecycle, so the conn hand-off to Publish is
// the single mutex swap it performs.
func (b *RedisBroker) supervise() {
	backoff := 250 * time.Millisecond
	for {
		if b.closed.Load() {
			return
		}
		err := b.connectRound()
		if b.closed.Load() {
			return
		}
		// A round that ENDED is by definition a dead bus: whatever err is,
		// the retry backoff applies (a round ending in "closed" has
		// already returned above).
		b.busErrors.Add(1)
		b.warnf("bus connection lost (%v); reconnecting in %v", err, backoff)
		select {
		case <-b.done:
			return
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > 5*time.Second {
			backoff = 5 * time.Second
		}
	}
}

// connectRound dials, authenticates, subscribes, then BLOCKS in the
// subscriber read loop until something fails. Returns the failure.
func (b *RedisBroker) connectRound() error {
	pub, sub, err := b.dialPair()
	if err != nil {
		return err
	}
	b.mu.Lock()
	if b.closed.Load() {
		b.mu.Unlock()
		pub.close()
		sub.close()
		return errors.New("broker: closed during connect")
	}
	b.pub, b.sub = pub, sub
	b.mu.Unlock()
	b.connected.Store(true)
	b.connectCount.Add(1)
	// Readiness means "a first connection is ESTABLISHED" — marked here,
	// not when the round ends: a healthy round blocks in the read loop
	// below for its whole (indefinite) lifetime.
	b.readyOnce.Do(func() { close(b.readyCh) })
	defer b.connected.Store(false)
	defer func() {
		b.mu.Lock()
		b.closeConnsLocked()
		b.mu.Unlock()
	}()

	err = b.readLoop(sub)
	pub.close()
	return err
}

// closeConnsLocked closes both conns if set. Callers hold b.mu.
func (b *RedisBroker) closeConnsLocked() {
	if b.pub != nil {
		b.pub.close()
		b.pub = nil
	}
	if b.sub != nil {
		b.sub.close()
		b.sub = nil
	}
}

// dialPair establishes both connections: TCP with a bounded handshake
// (dial 3 s, auth/select 5 s each), then the subscriber enters subscribe
// mode for every known topic.
func (b *RedisBroker) dialPair() (pub, sub *redisConn, err error) {
	dial := func() (*redisConn, error) {
		nc, err := net.DialTimeout("tcp", b.params.addr, 3*time.Second)
		if err != nil {
			return nil, err
		}
		c := &redisConn{nc: nc, br: bufio.NewReader(nc), bw: bufio.NewWriter(nc)}
		if err := c.handshake(b.params); err != nil {
			nc.Close()
			return nil, err
		}
		return c, nil
	}
	pub, err = dial()
	if err != nil {
		return nil, nil, fmt.Errorf("publisher dial: %w", err)
	}
	sub, err = dial()
	if err != nil {
		pub.close()
		return nil, nil, fmt.Errorf("subscriber dial: %w", err)
	}

	b.mu.Lock()
	topics := make([]string, 0, len(b.topics))
	for t := range b.topics {
		topics = append(topics, t)
	}
	b.mu.Unlock()
	for _, topic := range topics {
		if err := sub.writeCommand("SUBSCRIBE", topic); err != nil {
			pub.close()
			sub.close()
			return nil, nil, fmt.Errorf("subscribe %s: %w", topic, err)
		}
		// Redis answers each SUBSCRIBE with a confirmation array; consume
		// it here so the read loop only ever sees MESSAGES.
		if _, err := sub.readReply(5 * time.Second); err != nil {
			pub.close()
			sub.close()
			return nil, nil, fmt.Errorf("subscribe %s confirm: %w", topic, err)
		}
	}
	return pub, sub, nil
}

// readLoop consumes subscriber-mode replies until failure or Close.
// Shapes redis sends on a subscribed connection:
//
//	*3 ["message", <topic>, <payload>]  — a publication
//	*2 ["pmessage", ...]                — not used (we never PSUBSCRIBE)
//	*3 ["subscribe", <topic>, <n>]      — confirmations (consumed at dial)
//	*2 ["pong", ""]                     — answer to a keepalive PING
//
// Anything else is logged and skipped: a surplus reply must not kill the
// connection in a world where RESP arrays nest arbitrarily.
func (b *RedisBroker) readLoop(sub *redisConn) error {
	for {
		if b.closed.Load() {
			return errors.New("broker: closed")
		}
		reply, err := sub.readReply(0) // no read deadline: the socket IS the liveness check
		if err != nil {
			return fmt.Errorf("subscriber read: %w", err)
		}
		fields, ok := reply.([]any)
		if !ok || len(fields) < 3 {
			b.warnf("bus: unexpected reply shape %T, skipping", reply)
			continue
		}
		kind, _ := fields[0].(string)
		if kind != "message" {
			continue
		}
		topic, _ := fields[1].(string)
		payload, _ := fields[2].(string)
		b.busDeliver(topic, payload)
	}
}

// busDeliver unwraps one bus frame and delivers it locally — unless the
// frame is our OWN echo (redis delivers a publication to every subscriber
// of the topic, including this node's subscriber connection): the origin
// stamp is what makes exactly-once-per-node work with no message ids.
func (b *RedisBroker) busDeliver(topic, raw string) {
	var frame wireMessage
	if err := json.Unmarshal([]byte(raw), &frame); err != nil {
		b.busErrors.Add(1)
		b.warnf("bus: undecodable frame on %s (%d bytes), skipping", topic, len(raw))
		return
	}
	if frame.Origin == b.local.NodeID() {
		return // own echo: already delivered synchronously in Publish
	}
	payload, err := base64.StdEncoding.DecodeString(frame.Payload)
	if err != nil {
		b.busErrors.Add(1)
		b.warnf("bus: bad payload encoding on %s, skipping", topic)
		return
	}
	// Remote-hop Stats are not returned anywhere (fire-and-forget bus);
	// the local hub handler itself counts remote-homage into metrics.
	b.local.deliver(Envelope{
		Topic:   topic,
		Payload: json.RawMessage(payload),
		Origin:  frame.Origin,
		Local:   false,
		At:      time.Now(),
	})
}

// warnf is the nil-safe internal logger.
func (b *RedisBroker) warnf(format string, args ...any) {
	if b.logf != nil {
		b.logf(format, args...)
	}
}

// ---------------------------------------------------------------------------
// RESP (REdis Serialization Protocol) — the minimal correct subset.
// ---------------------------------------------------------------------------

// redisConn is one TCP connection speaking RESP. RESP is full-duplex, so
// reads and writes have independent serialization:
//
//   - writes go through mu (bufio writers are not concurrent-safe);
//   - reads are lock-free because exactly ONE goroutine per connection
//     ever reads — the publisher's round-tripper (whose write+reply pair
//     mu pairs up), or the subscriber's read loop.
//
// The split matters: a read parked on an idle subscribed socket must
// never block a live SUBSCRIBE/QUIT write on the same connection (a
// single "hold the lock across the read" design deadlocks there).
type redisConn struct {
	nc net.Conn
	br *bufio.Reader
	bw *bufio.Writer
	mu sync.Mutex // serializes writes; pairs write+reply on command paths
}

// handshake runs AUTH (when a password is set) and SELECT (when db != 0),
// each verified synchronously with a 5 s deadline.
func (c *redisConn) handshake(p redisParams) error {
	if p.password != "" {
		args := []string{"AUTH"}
		if p.user != "" {
			args = append(args, p.user)
		}
		args = append(args, p.password)
		if err := c.roundTrip(5*time.Second, args...); err != nil {
			return fmt.Errorf("auth: %w", err)
		}
	}
	if p.db != 0 {
		if err := c.roundTrip(5*time.Second, "SELECT", strconv.Itoa(p.db)); err != nil {
			return fmt.Errorf("select db %d: %w", p.db, err)
		}
	}
	return nil
}

// roundTrip writes one command and reads one reply, failing on RESP
// errors. Used for commands with simple-string replies (AUTH/SELECT).
func (c *redisConn) roundTrip(deadline time.Duration, args ...string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.writeCommandLocked(args...); err != nil {
		return err
	}
	reply, err := c.readReplyLocked(deadline)
	if err != nil {
		return err
	}
	if respErr, ok := reply.(*respError); ok {
		return respErr
	}
	return nil
}

// publish ships one PUBLISH and consumes its :recipients reply.
func (c *redisConn) publish(topic, message string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := c.writeCommandLocked("PUBLISH", topic, message); err != nil {
		return err
	}
	reply, err := c.readReplyLocked(5 * time.Second)
	if err != nil {
		return err
	}
	if respErr, ok := reply.(*respError); ok {
		return respErr
	}
	return nil
}

// writeCommand issues one command with internal serialization (subscriber
// connections command-write from exactly one goroutine, but symmetry
// keeps the API safe).
func (c *redisConn) writeCommand(args ...string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.writeCommandLocked(args...)
}

// writeCommandLocked emits the RESP array for one command:
//
//	*<n>\r\n$<len0>\r\n<arg0>\r\n…$<lenN>\r\n<argN>\r\n
func (c *redisConn) writeCommandLocked(args ...string) error {
	var sb strings.Builder
	fmt.Fprintf(&sb, "*%d\r\n", len(args))
	for _, arg := range args {
		fmt.Fprintf(&sb, "$%d\r\n%s\r\n", len(arg), arg)
	}
	if _, err := c.bw.WriteString(sb.String()); err != nil {
		return err
	}
	return c.bw.Flush()
}

// readReply parses one RESP value on the connection's single reader
// goroutine. deadline == 0 means "no deadline" (the subscriber's idle
// waiting; liveness is TCP-level there).
func (c *redisConn) readReply(deadline time.Duration) (any, error) {
	return c.readReplyLocked(deadline)
}

// readReplyLocked parses one RESP reply:
//
//	+OK            → string "OK"
//	-ERR ...       → *respError (an error VALUE, so mixed arrays work)
//	:123           → int64
//	$5\r\nhello    → string "hello" ($-1 → nil)
//	*3 ...         → []any (recursive; *-1 → nil)
func (c *redisConn) readReplyLocked(deadline time.Duration) (any, error) {
	if deadline > 0 {
		_ = c.nc.SetReadDeadline(time.Now().Add(deadline))
		defer c.nc.SetReadDeadline(time.Time{})
	}
	line, err := c.br.ReadString('\n')
	if err != nil {
		return nil, err
	}
	if len(line) < 3 || line[len(line)-2] != '\r' {
		return nil, fmt.Errorf("broker: malformed RESP line %q", truncate(line, 64))
	}
	payload := line[1 : len(line)-2]
	switch line[0] {
	case '+':
		return payload, nil
	case '-':
		return &respError{msg: payload}, nil
	case ':':
		n, err := strconv.ParseInt(payload, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("broker: bad RESP integer %q", payload)
		}
		return n, nil
	case '$':
		n, err := strconv.Atoi(payload)
		if err != nil {
			return nil, fmt.Errorf("broker: bad RESP bulk length %q", payload)
		}
		if n < 0 {
			return nil, nil // $-1 null
		}
		buf := make([]byte, n+2) // payload + trailing CRLF
		if _, err := io.ReadFull(c.br, buf); err != nil {
			return nil, err
		}
		return string(buf[:n]), nil
	case '*':
		n, err := strconv.Atoi(payload)
		if err != nil {
			return nil, fmt.Errorf("broker: bad RESP array length %q", payload)
		}
		if n < 0 {
			return nil, nil // *-1 null
		}
		items := make([]any, 0, n)
		for i := 0; i < n; i++ {
			// Recursive parse shares the raw line reader; deadlines apply
			// per-line via the outer call only (nested values arrive back-
			// to-back inside one frame, so the leading deadline covers).
			item, err := c.readReplyLocked(0)
			if err != nil {
				return nil, err
			}
			items = append(items, item)
		}
		return items, nil
	default:
		return nil, fmt.Errorf("broker: unknown RESP type byte %q", line[0])
	}
}

// close terminates the socket (idempotent via net.Conn semantics).
func (c *redisConn) close() { _ = c.nc.Close() }

// respError is a RESP "-ERR ..." line surfaced as an error value.
type respError struct{ msg string }

// Error implements error.
func (e *respError) Error() string { return "broker: redis error: " + e.msg }

// truncate bounds a value for log/error embedding.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
