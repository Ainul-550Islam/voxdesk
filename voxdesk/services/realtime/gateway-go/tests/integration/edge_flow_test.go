// Package integration exercises the gateway as one assembled system across
// package boundaries (config → auth → hub → server → protocol → presence →
// observability), the way a deploy actually wires it. Package-internal e2e
// tests live next to their packages; THIS test guards the seams between
// them — which is where package splits actually break things.
package integration

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/server"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

const (
	jwtSecret    = "0123456789abcdef0123456789abcdef"
	ingestSecret = "ingest-shared-secret-0123"
	tenantA      = "11111111-1111-1111-1111-111111111111"
	userA        = "99999999-8888-7777-6666-555555555555"
)

func testConfig() config.Config {
	return config.Config{
		Port:                          8790,
		JWTSecret:                     jwtSecret,
		JWTIssuer:                     "voxdesk",
		JWTAudience:                   "voxdesk-api",
		IngestSecret:                  ingestSecret,
		MaxConnections:                100,
		MaxConnsPerTenant:             10,
		MaxSubscriptionsPerConn:       8,
		OutgoingBuffer:                16,
		MaxMessageBytes:               16 * 1024,
		MaxIngestPayloadBytes:         4 * 1024,
		WriteWait:                     2 * time.Second,
		AuthTimeout:                   2 * time.Second,
		PingInterval:                  250 * time.Millisecond,
		PongTimeout:                   800 * time.Millisecond,
		ShutdownTimeout:               2 * time.Second,
		MessageRatePerSecond:          100,
		MessageBurst:                  100,
		IdempotencyTTL:                time.Minute,
		IdempotencyCapacity:           100,
		SignalingMaxSessionsPerTenant: 8,
		SignalingPendingTimeout:       time.Minute,
	}
}

func mintToken(t *testing.T, claims map[string]any) string {
	t.Helper()
	header, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	body, _ := json.Marshal(claims)
	head := base64.RawURLEncoding.EncodeToString(header)
	payload := base64.RawURLEncoding.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(jwtSecret))
	mac.Write([]byte(head + "." + payload))
	return head + "." + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func claims(ttl time.Duration) map[string]any {
	now := time.Now()
	return map[string]any{
		"sub": userA, "tid": tenantA, "role": "admin", "tv": float64(1),
		"typ": "access", "iat": float64(now.Unix()), "nbf": float64(now.Unix()),
		"exp": float64(now.Add(ttl).Unix()), "iss": "voxdesk", "aud": "voxdesk-api", "jti": "int-1",
	}
}

// captureEmitter records structured events for assertions.
type captureEmitter struct {
	mu   sync.Mutex
	seen []observability.Event
}

func (c *captureEmitter) Emit(e observability.Event) {
	c.mu.Lock()
	c.seen = append(c.seen, e)
	c.mu.Unlock()
}

func (c *captureEmitter) find(name string) *observability.Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.seen {
		if c.seen[i].Name == name {
			return &c.seen[i]
		}
	}
	return nil
}

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

// TestAssembledEdgeLoop is the cross-package proof: a dashboard session and
// an API publish traverse FOUR packages (auth/hub/server/protocol), surface
// in TWO observability planes (request-id echo, structured event + presence
// gauge), and never cross a tenant boundary anywhere along the way.
func TestAssembledEdgeLoop(t *testing.T) {
	t.Parallel()

	cfg := testConfig()
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sessions := session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
	signaler := signaling.NewRouter(sessions, h.Lookup, reg)
	srv := server.New(cfg, h, reg, verifier, signaler)
	events := &captureEmitter{}
	srv.UseEmitter(events)

	httpSrv := httptest.NewServer(srv.Handler())
	t.Cleanup(httpSrv.Close)
	t.Cleanup(srv.CloseAll)

	// --- browser side ---
	wsURL := "ws" + strings.TrimPrefix(httpSrv.URL, "http") + "/ws"
	conn, _, err := gorilla.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	if welcome := readFrame(t, conn); welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, claims(5*time.Minute))
	if err := conn.WriteMessage(gorilla.TextMessage, []byte(fmt.Sprintf(`{"type":"hello","token":%q}`, token))); err != nil {
		t.Fatalf("hello: %v", err)
	}
	if ready := readFrame(t, conn); ready["type"] != "ready" || ready["tenant_id"] != tenantA {
		t.Fatalf("ready = %v", ready)
	}
	if err := conn.WriteMessage(gorilla.TextMessage, []byte(`{"type":"subscribe","room":"calls"}`)); err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	if sub := readFrame(t, conn); sub["type"] != "subscribed" {
		t.Fatalf("subscribed = %v", sub)
	}

	// The JWT's sub made the user present on this node — presence is a
	// real registry reachable across the package boundary, not a stub.
	if !srv.Presence().IsOnline(tenantA, userA) {
		t.Fatal("verified identity must appear in presence after hello")
	}
	if reg.PresenceUsersCurrent() != 1 {
		t.Fatalf("presence gauge must track transitions, got %d", reg.PresenceUsersCurrent())
	}

	// --- API side, with a correlation id the way the real API sends it ---
	req, err := http.NewRequest(http.MethodPost, httpSrv.URL+"/ingest/v1/publish",
		strings.NewReader(fmt.Sprintf(`{"tenant_id":%q,"room":"calls","kind":"call.updated","payload":{"call_id":"int-call-1"}}`, tenantA)))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+ingestSecret)
	req.Header.Set("X-Request-ID", "api-trace-abc-123")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("ingest POST: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("ingest status = %d", resp.StatusCode)
	}
	if got := resp.Header.Get("X-Request-ID"); got != "api-trace-abc-123" {
		t.Fatalf("the API's request id must echo back unchanged, got %q", got)
	}

	// --- the browser hears its tenant's event ---
	delivery := readFrame(t, conn)
	if delivery["type"] != "delivery" || delivery["kind"] != "call.updated" {
		t.Fatalf("delivery = %v", delivery)
	}
	payload, _ := delivery["payload"].(map[string]any)
	if payload["call_id"] != "int-call-1" {
		t.Fatalf("payload corrupted: %v", payload)
	}

	// --- the event stream carries the SAME correlation id ---
	accepted := events.find("ingest.accepted")
	if accepted == nil {
		t.Fatal("no ingest.accepted event emitted")
	}
	if accepted.Field("request_id") != "api-trace-abc-123" || accepted.Field("tenant") != tenantA || accepted.Field("delivered") != "1" {
		t.Fatalf("event fields wrong: %+v", accepted.Fields)
	}

	// --- teardown flips presence back, gauge included ---
	conn.Close()
	deadline := time.Now().Add(3 * time.Second)
	for srv.Presence().IsOnline(tenantA, userA) && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if srv.Presence().IsOnline(tenantA, userA) {
		t.Fatal("presence must drop when the last session of a user closes")
	}
	deadline = time.Now().Add(3 * time.Second)
	for reg.PresenceUsersCurrent() != 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if reg.PresenceUsersCurrent() != 0 {
		t.Fatalf("gauge must follow transitions down too, got %d", reg.PresenceUsersCurrent())
	}
}
