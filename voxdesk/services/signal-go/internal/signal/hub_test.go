package signal

import (
	"encoding/json"
	"errors"
	"net"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// --- helpers ---------------------------------------------------------------

func newTestServer(t *testing.T, idle time.Duration) (*httptest.Server, *Hub) {
	t.Helper()
	hub := NewHub()
	srv := NewServer(hub)
	srv.idle = idle
	ts := httptest.NewServer(srv)
	t.Cleanup(ts.Close)
	return ts, hub
}

func dial(t *testing.T, ts *httptest.Server) *websocket.Conn {
	t.Helper()
	url := "ws" + strings.TrimPrefix(ts.URL, "http")
	conn, _, err := websocket.DefaultDialer.Dial(url, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func sendJSON(t *testing.T, conn *websocket.Conn, v any) {
	t.Helper()
	if err := conn.WriteJSON(v); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// readType reads frames until it sees one of wantType, returning its raw
// fields.
func readType(t *testing.T, conn *websocket.Conn, wantType string) map[string]json.RawMessage {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	for i := 0; i < 20; i++ {
		_, data, err := conn.ReadMessage()
		if err != nil {
			t.Fatalf("read (want %s): %v", wantType, err)
		}
		var m map[string]json.RawMessage
		if err := json.Unmarshal(data, &m); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if string(m["type"]) == `"`+wantType+`"` {
			return m
		}
	}
	t.Fatalf("did not see %s within 20 frames", wantType)
	return nil
}

// checkErrorCode asserts the next error frame carries the given code.
func checkErrorCode(t *testing.T, conn *websocket.Conn, wantCode string) {
	t.Helper()
	m := readType(t, conn, TypeError)
	if strings.Trim(string(m["code"]), `"`) != wantCode {
		t.Fatalf("error code = %s, want %s", m["code"], wantCode)
	}
}

// expectNoMessage asserts that no frame arrives within a short quiet window.
func expectNoMessage(t *testing.T, conn *websocket.Conn) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
	_, _, err := conn.ReadMessage()
	if err == nil {
		t.Fatal("expected no message, but received one")
	}
	var netErr net.Error
	if !errors.As(err, &netErr) || !netErr.Timeout() {
		t.Fatalf("expected read timeout, got %v", err)
	}
	_ = conn.SetReadDeadline(time.Time{})
}

// expectClose asserts that the server closed the connection.
func expectClose(t *testing.T, conn *websocket.Conn) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, _, err := conn.ReadMessage(); err == nil {
		t.Fatal("expected the connection to be closed")
	}
}

func waitFor(t *testing.T, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal(msg)
}

// --- protocol-level tests ---------------------------------------------------

func TestWelcomeIsFirstFrame(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	c := dial(t, ts)
	m := readType(t, c, TypeWelcome)
	sid := strings.Trim(string(m["session_id"]), `"`)
	if sid == "" {
		t.Fatal("empty session id")
	}
	if len(strings.Split(sid, "-")) != 5 {
		t.Fatalf("session id %q is not a UUID", sid)
	}
}

func TestSubscribeRequiresHello(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	c := dial(t, ts)
	readType(t, c, TypeWelcome)
	sendJSON(t, c, map[string]any{"type": TypeSubscribe, "room": "r"})
	checkErrorCode(t, c, CodeHelloRequired)
}

func TestPublishRequiresHello(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	c := dial(t, ts)
	readType(t, c, TypeWelcome)
	sendJSON(t, c, map[string]any{"type": TypePublish, "room": "r", "payload": map[string]any{}})
	checkErrorCode(t, c, CodeHelloRequired)
}

func TestHelloExactlyOnce(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	c := dial(t, ts)
	readType(t, c, TypeWelcome)

	sendJSON(t, c, map[string]any{"type": TypeHello, "tenant_id": "t"})
	// A successful hello produces no reply frame; a ping flushes and, if an
	// error frame had been queued, readType would trip over it.
	sendJSON(t, c, map[string]any{"type": TypePing})
	readType(t, c, TypePong)

	sendJSON(t, c, map[string]any{"type": TypeHello, "tenant_id": "t2"})
	checkErrorCode(t, c, CodeHelloInvalid)

	// A fresh connection with an empty tenant id is also rejected.
	c2 := dial(t, ts)
	readType(t, c2, TypeWelcome)
	sendJSON(t, c2, map[string]any{"type": TypeHello, "tenant_id": ""})
	checkErrorCode(t, c2, CodeHelloInvalid)
}

func TestPublishDeliversToPeersNotSender(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	a := dial(t, ts)
	b := dial(t, ts)

	readType(t, a, TypeWelcome)
	bw := readType(t, b, TypeWelcome)
	sessionB := strings.Trim(string(bw["session_id"]), `"`)

	sendJSON(t, a, map[string]any{"type": TypeHello, "tenant_id": "t-1"})
	sendJSON(t, b, map[string]any{"type": TypeHello, "tenant_id": "t-1"})

	sendJSON(t, a, map[string]any{"type": TypeSubscribe, "room": "r"})
	if sub := readType(t, a, TypeSubscribed); string(sub["peers"]) != "1" {
		t.Fatalf("a peers = %s, want 1", sub["peers"])
	}
	sendJSON(t, b, map[string]any{"type": TypeSubscribe, "room": "r"})
	if sub := readType(t, b, TypeSubscribed); string(sub["peers"]) != "2" {
		t.Fatalf("b peers = %s, want 2", sub["peers"])
	}

	sendJSON(t, b, map[string]any{"type": TypePublish, "room": "r", "payload": map[string]any{"x": 1}})
	del := readType(t, a, TypeDelivery)
	if strings.Trim(string(del["from"]), `"`) != sessionB {
		t.Fatalf("delivery from = %s, want %s", del["from"], sessionB)
	}
	if strings.Trim(string(del["room"]), `"`) != "r" {
		t.Fatalf("delivery room = %s", del["room"])
	}
	// The sender never receives its own payload.
	expectNoMessage(t, b)
}

func TestTenantIsolation(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	a := dial(t, ts)
	b := dial(t, ts)
	readType(t, a, TypeWelcome)
	readType(t, b, TypeWelcome)

	sendJSON(t, a, map[string]any{"type": TypeHello, "tenant_id": "tenant-a"})
	sendJSON(t, b, map[string]any{"type": TypeHello, "tenant_id": "tenant-b"})
	sendJSON(t, a, map[string]any{"type": TypeSubscribe, "room": "shared"})
	readType(t, a, TypeSubscribed)
	sendJSON(t, b, map[string]any{"type": TypeSubscribe, "room": "shared"})
	readType(t, b, TypeSubscribed)

	sendJSON(t, a, map[string]any{"type": TypePublish, "room": "shared", "payload": map[string]any{"x": 1}})
	// b is a different tenant under the same room name: nothing may cross.
	expectNoMessage(t, b)
	expectNoMessage(t, a)
}

func TestUnsubscribeStopsDelivery(t *testing.T) {
	ts, hub := newTestServer(t, time.Minute)
	a := dial(t, ts)
	b := dial(t, ts)
	readType(t, a, TypeWelcome)
	readType(t, b, TypeWelcome)

	sendJSON(t, a, map[string]any{"type": TypeHello, "tenant_id": "t"})
	sendJSON(t, b, map[string]any{"type": TypeHello, "tenant_id": "t"})
	sendJSON(t, a, map[string]any{"type": TypeSubscribe, "room": "r"})
	readType(t, a, TypeSubscribed)
	sendJSON(t, b, map[string]any{"type": TypeSubscribe, "room": "r"})
	readType(t, b, TypeSubscribed)

	sendJSON(t, a, map[string]any{"type": TypeUnsubscribe, "room": "r"})
	if un := readType(t, a, TypeUnsubscribed); strings.Trim(string(un["room"]), `"`) != "r" {
		t.Fatalf("unsubscribed room = %s", un["room"])
	}

	sendJSON(t, b, map[string]any{"type": TypePublish, "room": "r", "payload": map[string]any{}})
	expectNoMessage(t, a)

	if got := hub.roomPeers("t", "r"); got != 1 {
		t.Fatalf("room peers = %d, want 1", got)
	}
}

func TestBadMessageResponses(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	c := dial(t, ts)
	readType(t, c, TypeWelcome)

	_ = c.WriteMessage(websocket.TextMessage, []byte("{{{not json"))
	checkErrorCode(t, c, CodeBadMessage)

	sendJSON(t, c, map[string]any{"type": "bogus"})
	checkErrorCode(t, c, CodeBadMessage)

	sendJSON(t, c, map[string]any{"type": "subscribe"})
	checkErrorCode(t, c, CodeBadMessage)
}

func TestPingPong(t *testing.T) {
	ts, _ := newTestServer(t, time.Minute)
	c := dial(t, ts)
	readType(t, c, TypeWelcome)
	sendJSON(t, c, map[string]any{"type": TypePing})
	readType(t, c, TypePong)
}

func TestDisconnectCleansUpRoom(t *testing.T) {
	ts, hub := newTestServer(t, time.Minute)
	a := dial(t, ts)
	b := dial(t, ts)
	readType(t, a, TypeWelcome)
	readType(t, b, TypeWelcome)

	sendJSON(t, a, map[string]any{"type": TypeHello, "tenant_id": "t"})
	sendJSON(t, b, map[string]any{"type": TypeHello, "tenant_id": "t"})
	sendJSON(t, a, map[string]any{"type": TypeSubscribe, "room": "r"})
	readType(t, a, TypeSubscribed)
	sendJSON(t, b, map[string]any{"type": TypeSubscribe, "room": "r"})
	readType(t, b, TypeSubscribed)

	if got := hub.roomPeers("t", "r"); got != 2 {
		t.Fatalf("room peers = %d, want 2", got)
	}
	_ = a.Close()

	waitFor(t, func() bool { return hub.roomPeers("t", "r") == 1 }, "room did not shrink to 1 peer")
	waitFor(t, func() bool { return hub.sessionCount() == 1 }, "session count did not shrink to 1")
}

func TestIdleReap(t *testing.T) {
	ts, _ := newTestServer(t, 150*time.Millisecond)
	c := dial(t, ts)
	readType(t, c, TypeWelcome)
	sendJSON(t, c, map[string]any{"type": TypeHello, "tenant_id": "t"})
	// No further traffic: the server reaps the idle session and the client's
	// next read observes the closed connection.
	expectClose(t, c)
}

// --- hub-level tests (no network) ------------------------------------------

func TestBackpressureDropsWhenBufferFull(t *testing.T) {
	h := NewHub()
	cha := h.register("a")
	chb := h.register("b")
	h.hello("a", "t")
	h.hello("b", "t")
	h.subscribe("a", "r")
	h.subscribe("b", "r")

	for i := 0; i < outgoingBuffer; i++ {
		if !trySend(chb, newPong()) {
			t.Fatalf("could not fill b's buffer at %d", i)
		}
	}
	// a publishes; the only peer is b, whose queue is full, so the delivery
	// is dropped and the sender is never blocked.
	if got := h.publish("a", "r", json.RawMessage(`{"x":1}`)); got != 0 {
		t.Fatalf("delivered = %d, want 0 (b's queue is full)", got)
	}
	// a must not have received anything (no self-delivery).
	select {
	case <-cha:
		t.Fatal("a received a frame it should not have")
	default:
	}
}

func TestPublishCountsDeliveries(t *testing.T) {
	h := NewHub()
	h.register("a")
	h.register("b")
	h.register("c")
	h.hello("a", "t")
	h.hello("b", "t")
	h.hello("c", "t")
	h.subscribe("a", "r")
	h.subscribe("b", "r")
	h.subscribe("c", "r")

	// a -> b and c (two peers, sender excluded).
	if got := h.publish("a", "r", json.RawMessage(`{}`)); got != 2 {
		t.Fatalf("delivered = %d, want 2", got)
	}
	// a -> empty room: no peers, no error.
	if got := h.publish("a", "missing", json.RawMessage(`{}`)); got != 0 {
		t.Fatalf("delivered = %d, want 0", got)
	}
}

// --- concurrency smoke test (run with -race) --------------------------------

func TestConcurrentPublishTenantIsolation(t *testing.T) {
	ts, _ := newTestServer(t, 5*time.Second)
	const n = 12
	const publishes = 25

	type client struct {
		conn     *websocket.Conn
		session  string
		tenant   string
		received []string // "from" values of deliveries
	}

	clients := make([]*client, n)
	for i := 0; i < n; i++ {
		tenant := "tenant-a"
		if i >= n/2 {
			tenant = "tenant-b"
		}
		clients[i] = &client{conn: dial(t, ts), tenant: tenant}
	}

	// Sequential setup: hello + subscribe, recording session ids.
	for _, c := range clients {
		w := readType(t, c.conn, TypeWelcome)
		c.session = strings.Trim(string(w["session_id"]), `"`)
		sendJSON(t, c.conn, map[string]any{"type": TypeHello, "tenant_id": c.tenant})
		sendJSON(t, c.conn, map[string]any{"type": TypeSubscribe, "room": "shared"})
		readType(t, c.conn, TypeSubscribed)
	}

	tenantOf := make(map[string]string, n)
	for _, c := range clients {
		tenantOf[c.session] = c.tenant
	}

	var wg sync.WaitGroup
	for _, c := range clients {
		wg.Add(1)
		go func(c *client) {
			defer wg.Done()
			for p := 0; p < publishes; p++ {
				_ = c.conn.WriteJSON(map[string]any{
					"type":    TypePublish,
					"room":    "shared",
					"payload": map[string]any{"from": c.session, "seq": p},
				})
			}
			_ = c.conn.SetReadDeadline(time.Now().Add(3 * time.Second))
			for {
				_, data, err := c.conn.ReadMessage()
				if err != nil {
					break
				}
				var m map[string]json.RawMessage
				if json.Unmarshal(data, &m) != nil {
					continue
				}
				if string(m["type"]) != `"delivery"` {
					continue
				}
				c.received = append(c.received, strings.Trim(string(m["from"]), `"`))
			}
		}(c)
	}
	wg.Wait()

	total := 0
	for _, c := range clients {
		for _, from := range c.received {
			total++
			if from == c.session {
				t.Fatalf("client %s received its own delivery", c.session)
			}
			if tenantOf[from] != c.tenant {
				t.Fatalf("cross-tenant delivery: %s (tenant %s) -> %s (tenant %s)",
					from, tenantOf[from], c.session, c.tenant)
			}
		}
	}
	if total == 0 {
		t.Fatal("no deliveries at all: fan-out is broken")
	}
}
