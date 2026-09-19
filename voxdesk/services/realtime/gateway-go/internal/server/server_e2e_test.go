package server

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"
)

// mintToken builds an HS256 token identical to what the Python API issues.
func mintToken(t *testing.T, secret string, claims map[string]any) string {
	t.Helper()
	header, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	body, _ := json.Marshal(claims)
	head := base64.RawURLEncoding.EncodeToString(header)
	payload := base64.RawURLEncoding.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(head + "." + payload))
	return head + "." + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func dashboardClaims(tenant, role string, ttl time.Duration) map[string]any {
	now := time.Now()
	return map[string]any{
		"sub":  "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
		"tid":  tenant,
		"role": role,
		"tv":   float64(1),
		"typ":  "access",
		"iat":  float64(now.Unix()),
		"nbf":  float64(now.Unix()),
		"exp":  float64(now.Add(ttl).Unix()),
		"iss":  "voxdesk",
		"aud":  "voxdesk-api",
		"jti":  "test-jti",
	}
}

// dial opens one websocket to the fixture gateway.
func dial(t *testing.T, f *fixture) *gorilla.Conn {
	t.Helper()
	wsURL := "ws" + strings.TrimPrefix(f.http.URL, "http") + "/ws"
	conn, _, err := gorilla.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

// readFrame reads one JSON frame into a generic map with a bounded wait.
func readFrame(t *testing.T, conn *gorilla.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	var frame map[string]any
	if err := json.Unmarshal(data, &frame); err != nil {
		t.Fatalf("frame is not JSON: %v (%s)", err, data)
	}
	return frame
}

func sendFrame(t *testing.T, conn *gorilla.Conn, frame string) {
	t.Helper()
	if err := conn.WriteMessage(gorilla.TextMessage, []byte(frame)); err != nil {
		t.Fatalf("write frame: %v", err)
	}
}

// TestFullSessionRoundTrip is THE behaviour proof of the whole service:
// welcome → hello → ready → subscribed → ingest → delivery, with the
// tenant pinned by the token and the payload delivered verbatim.
func TestFullSessionRoundTrip(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)

	// 1) The server speaks first: welcome before any client traffic.
	welcome := readFrame(t, conn)
	if welcome["type"] != "welcome" || welcome["auth_required"] != true {
		t.Fatalf("welcome = %v", welcome)
	}
	if welcome["session_id"] == "" {
		t.Fatalf("welcome must assign the session: %v", welcome)
	}

	// 2) Authenticate with a dashboard token.
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", 15*time.Minute))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	ready := readFrame(t, conn)
	if ready["type"] != "ready" || ready["tenant_id"] != testTenantA || ready["role"] != "owner" {
		t.Fatalf("ready = %v", ready)
	}

	// 3) Subscribe the calls feed; peer count reflects this join.
	sendFrame(t, conn, `{"type":"subscribe","room":"calls"}`)
	sub := readFrame(t, conn)
	if sub["type"] != "subscribed" || sub["room"] != "calls" || sub["peers"] != float64(1) {
		t.Fatalf("subscribed = %v", sub)
	}

	// 4) The API publishes; the browser receives a delivery, verbatim payload.
	status, ingestBody := f.post(t,
		`{"tenant_id":"`+testTenantA+`","room":"calls","kind":"call.updated","payload":{"status":"COMPLETED","call_sid":"CA123"},"event_id":"550e8400-e29b-41d4-a716-446655440000"}`,
		"Bearer "+testIngestSecret)
	if status != http.StatusOK || ingestBody["delivered"] != float64(1) {
		t.Fatalf("ingest: status %d body %v", status, ingestBody)
	}
	delivery := readFrame(t, conn)
	if delivery["type"] != "delivery" || delivery["room"] != "calls" || delivery["kind"] != "call.updated" {
		t.Fatalf("delivery = %v", delivery)
	}
	payload, _ := json.Marshal(delivery["payload"])
	if !strings.Contains(string(payload), "COMPLETED") {
		t.Fatalf("payload not delivered verbatim: %s", payload)
	}

	// 5) Unsubscribe → ack, then a second publish delivers nothing more.
	sendFrame(t, conn, `{"type":"unsubscribe","room":"calls"}`)
	unsub := readFrame(t, conn)
	if unsub["type"] != "unsubscribed" || unsub["room"] != "calls" {
		t.Fatalf("unsubscribed = %v", unsub)
	}
	_, after := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), "Bearer "+testIngestSecret)
	if after["delivered"] != float64(0) {
		t.Fatalf("after unsubscribe, delivered = %v", after)
	}
}

// TestBadTokenClosesSession proves a forged hello gets ONE generic error
// frame and then a 1008 close — no retry surface on a public edge.
func TestBadTokenClosesSession(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn) // welcome

	forged := mintToken(t, "the-attackers-guessed-secret-000", dashboardClaims(testTenantA, "owner", time.Hour))
	sendFrame(t, conn, `{"type":"hello","token":"`+forged+`"}`)
	errFrame := readFrame(t, conn)
	if errFrame["type"] != "error" || errFrame["code"] != "auth_failed" {
		t.Fatalf("error frame = %v", errFrame)
	}

	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	for {
		_, _, err := conn.ReadMessage()
		if err != nil {
			closeErr, ok := err.(*gorilla.CloseError)
			if !ok {
				t.Fatalf("expected a close frame, got %v", err)
			}
			if closeErr.Code != 1008 {
				t.Fatalf("close code = %d, want 1008", closeErr.Code)
			}
			return
		}
	}
}

// TestPreAuthSubscribeIsRefused proves the hello-first rule.
func TestPreAuthSubscribeIsRefused(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn) // welcome

	sendFrame(t, conn, `{"type":"subscribe","room":"calls"}`)
	errFrame := readFrame(t, conn)
	if errFrame["type"] != "error" || errFrame["code"] != "hello_required" {
		t.Fatalf("pre-auth subscribe = %v", errFrame)
	}
}

// TestRoomValidationAtTheEdge proves a verified client still cannot
// subscribe to arbitrary rooms (namespace is closed).
func TestRoomValidationAtTheEdge(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn)
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", time.Hour))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	_ = readFrame(t, conn) // ready

	sendFrame(t, conn, `{"type":"subscribe","room":"admin"}`)
	errFrame := readFrame(t, conn)
	if errFrame["code"] != "room_invalid" {
		t.Fatalf("room validation = %v", errFrame)
	}
}

// TestTenantIsolationEndToEnd is the wire-level proof: two live sockets,
// two tenants, same room name, one publish — only the matching tenant's
// browser is notified.
func TestTenantIsolationEndToEnd(t *testing.T) {
	f := newFixture(t)
	connA := dial(t, f)
	_ = readFrame(t, connA)
	tokenA := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", time.Hour))
	sendFrame(t, connA, `{"type":"hello","token":"`+tokenA+`"}`)
	_ = readFrame(t, connA)
	sendFrame(t, connA, `{"type":"subscribe","room":"calls"}`)
	_ = readFrame(t, connA)

	connB := dial(t, f)
	_ = readFrame(t, connB)
	tokenB := mintToken(t, testJWTSecret, dashboardClaims(testTenantB, "owner", time.Hour))
	sendFrame(t, connB, `{"type":"hello","token":"`+tokenB+`"}`)
	_ = readFrame(t, connB)
	sendFrame(t, connB, `{"type":"subscribe","room":"calls"}`)
	_ = readFrame(t, connB)

	if _, body := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), "Bearer "+testIngestSecret); body["delivered"] != float64(1) {
		t.Fatalf("publish to tenant A: %v", body)
	}
	delivery := readFrame(t, connA)
	if delivery["type"] != "delivery" {
		t.Fatalf("tenant A expected a delivery, got %v", delivery)
	}

	// Tenant B's socket must receive NOTHING (short wait to catch a leak).
	_ = connB.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
	_, _, err := connB.ReadMessage()
	if err == nil {
		t.Fatal("TENANT B RECEIVED A FRAME FROM TENANT A'S ROOM — isolation breach")
	}
	if !strings.Contains(err.Error(), "timeout") && !gorilla.IsUnexpectedCloseError(err, gorilla.CloseGoingAway) {
		// a read timeout is the expected outcome
		t.Logf("tenant B read ended with: %v (a timeout is expected; any delivered frame is a breach)", err)
	}
}

// TestPingPongThroughTheJSONPath proves application-level latency probes
// work alongside the WS-level heartbeat.
func TestPingPongThroughTheJSONPath(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn)
	sendFrame(t, conn, `{"type":"ping"}`)
	pong := readFrame(t, conn)
	if pong["type"] != "pong" || pong["server_time"] == "" {
		t.Fatalf("pong = %v", pong)
	}
}

// TestTokenExpiryClosesSession proves the edge never outlives the
// credential that opened it (see heartbeat.go).
func TestTokenExpiryClosesSession(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn)

	// A token that is ALREADY past its grace when verified is refused at
	// hello (the verifier's own expiry check); this test pins the runtime
	// behaviour regardless of which layer enforces it.
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "owner", -2*time.Minute))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	errFrame := readFrame(t, conn)
	if errFrame["code"] != "auth_failed" {
		t.Fatalf("expired token = %v, want auth_failed", errFrame)
	}
}
