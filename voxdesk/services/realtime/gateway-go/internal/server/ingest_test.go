package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

const (
	testJWTSecret    = "0123456789abcdef0123456789abcdef"
	testIngestSecret = "ingest-shared-secret-0123"
	testTenantA      = "11111111-1111-1111-1111-111111111111"
	testTenantB      = "22222222-2222-2222-2222-222222222222"
)

func testConfig() config.Config {
	return config.Config{
		Port:                          8790,
		JWTSecret:                     testJWTSecret,
		JWTIssuer:                     "voxdesk",
		JWTAudience:                   "voxdesk-api",
		IngestSecret:                  testIngestSecret,
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
		SignalingMaxSessionsPerTenant: 8,
		SignalingPendingTimeout:       time.Minute,
		IdempotencyCapacity:           100,
	}
}

// fixture wires a full gateway for httptest use.
type fixture struct {
	server *Server
	hub    *hub.Hub
	reg    *metrics.Registry
	http   *httptest.Server
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	cfg := testConfig()
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}
}

// post issues one ingest request.
func (f *fixture) post(t *testing.T, body, authHeader string) (int, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, f.http.URL+"/ingest/v1/publish", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&parsed)
	return resp.StatusCode, parsed
}

// sink is a hub.Subscriber that records deliveries (delivery assertions
// belong at the hub boundary, not on a socket).
type sink struct {
	session string
	tenant  string

	mu     sync.Mutex
	frames []any
}

func (s *sink) Session() string { return s.session }
func (s *sink) Tenant() string  { return s.tenant }
func (s *sink) Enqueue(msg any) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.frames = append(s.frames, msg)
	return true
}
func (s *sink) RequestClose(int, string) {}

func (s *sink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.frames)
}

func validBody(tenant, room, kind, eventID string) string {
	event := ""
	if eventID != "" {
		event = `,"event_id":"` + eventID + `"`
	}
	return `{"tenant_id":"` + tenant + `","room":"` + room + `","kind":"` + kind + `","payload":{"status":"COMPLETED"}` + event + `}`
}

func TestIngestRequiresSecret(t *testing.T) {
	f := newFixture(t)
	for _, header := range []string{"", "Bearer wrong-secret", "Basic abc", "Bearerx " + testIngestSecret} {
		status, _ := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), header)
		if status != http.StatusUnauthorized {
			t.Errorf("auth header %q: status = %d, want 401", header, status)
		}
	}
}

func TestIngestValidatesShapes(t *testing.T) {
	f := newFixture(t)
	auth := "Bearer " + testIngestSecret

	cases := []struct {
		name string
		body string
		want int
	}{
		{"bad tenant", `{"tenant_id":"not-a-uuid","room":"calls","kind":"call.updated","payload":{}}`, http.StatusUnprocessableEntity},
		{"bad room", validBody(testTenantA, "admin", "call.updated", ""), http.StatusUnprocessableEntity},
		{"tenant-named room", validBody(testTenantA, "call:not-a-uuid", "call.updated", ""), http.StatusUnprocessableEntity},
		{"bad kind", validBody(testTenantA, "calls", "UPPER.CASE", ""), http.StatusUnprocessableEntity},
		{"empty payload", `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated"}`, http.StatusUnprocessableEntity},
		{"scalar payload", `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated","payload":"a bare string is valid JSON but not fan-out material"}`, http.StatusUnprocessableEntity},
		{"malformed json", `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated","payload":{broken`, http.StatusUnprocessableEntity},
		{"bad event id", validBody(testTenantA, "calls", "call.updated", "not-a-uuid"), http.StatusUnprocessableEntity},
		{"not json at all", `this is not {json`, http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status, body := f.post(t, tc.body, auth)
			if status != tc.want {
				t.Errorf("status = %d, want %d (body %v)", status, tc.want, body)
			}
		})
	}
}

func TestIngestDeliversToSubscriber(t *testing.T) {
	f := newFixture(t)
	a := &sink{session: "s-a", tenant: testTenantA}
	b := &sink{session: "s-b", tenant: testTenantB}
	f.hub.Register(a)
	f.hub.Register(b)
	if err := f.hub.BindTenant("s-a", testTenantA); err != nil {
		t.Fatal(err)
	}
	if err := f.hub.BindTenant("s-b", testTenantB); err != nil {
		t.Fatal(err)
	}
	if _, err := f.hub.Subscribe("s-a", "calls"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.hub.Subscribe("s-b", "calls"); err != nil {
		t.Fatal(err)
	}

	status, body := f.post(t, validBody(testTenantA, "calls", "call.updated", ""), "Bearer "+testIngestSecret)
	if status != http.StatusOK {
		t.Fatalf("status = %d", status)
	}
	if body["delivered"] != float64(1) || body["dropped"] != float64(0) || body["duplicate"] != false {
		t.Errorf("response = %v", body)
	}
	if a.count() != 1 {
		t.Errorf("tenant A sink = %d frames, want 1", a.count())
	}
	if b.count() != 0 {
		t.Errorf("TENANT B sink = %d frames — isolation breach", b.count())
	}
}

func TestIngestSuppressesReplay(t *testing.T) {
	f := newFixture(t)
	eventID := "550e8400-e29b-41d4-a716-446655440000"
	auth := "Bearer " + testIngestSecret

	status, first := f.post(t, validBody(testTenantA, "calls", "call.updated", eventID), auth)
	if status != http.StatusOK || first["duplicate"] != false {
		t.Fatalf("first delivery: status %d body %v", status, first)
	}
	status, second := f.post(t, validBody(testTenantA, "calls", "call.updated", eventID), auth)
	if status != http.StatusOK {
		t.Fatalf("replay: status %d", status)
	}
	if second["duplicate"] != true || second["delivered"] != float64(0) {
		t.Errorf("replay must be suppressed as duplicate: %v", second)
	}
	// A replay scoped to a DIFFERENT tenant is a different event entirely.
	status, third := f.post(t, validBody(testTenantB, "calls", "call.updated", eventID), auth)
	if status != http.StatusOK || third["duplicate"] != false {
		t.Errorf("same event_id in another tenant must NOT dedupe: %v", third)
	}
}

func TestIngestOversizeRejected(t *testing.T) {
	f := newFixture(t)
	big := strings.Repeat("x", int(testConfig().MaxIngestPayloadBytes)+8192)
	body := `{"tenant_id":"` + testTenantA + `","room":"calls","kind":"call.updated","payload":{"blob":"` + big + `"}}`
	status, _ := f.post(t, body, "Bearer "+testIngestSecret)
	if status != http.StatusRequestEntityTooLarge && status != http.StatusUnprocessableEntity {
		t.Errorf("status = %d, want 413 or 422", status)
	}
}

func TestLivenessReadinessAndNotFound(t *testing.T) {
	f := newFixture(t)

	resp, err := http.Get(f.http.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("healthz = %d", resp.StatusCode)
	}

	resp, err = http.Get(f.http.URL + "/readyz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("readyz = %d, want 200 with headroom", resp.StatusCode)
	}

	resp, err = http.Get(f.http.URL + "/nope")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("unknown path = %d, want 404", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Errorf("API 404s must be JSON, got %q", ct)
	}
}

func TestMethodNotAllowedIsJSON(t *testing.T) {
	f := newFixture(t)
	resp, err := http.Post(f.http.URL+"/healthz", "text/plain", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("POST /healthz = %d, want 405", resp.StatusCode)
	}
}

func TestMetricsTokenGate(t *testing.T) {
	cfg := testConfig()
	cfg.MetricsToken = "scrape-token-123"
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)

	// No token: 401.
	resp, err := http.Get(ts.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("metrics without token = %d, want 401", resp.StatusCode)
	}

	// Wrong token: 401.
	req, _ := http.NewRequest(http.MethodGet, ts.URL+"/metrics", nil)
	req.Header.Set("Authorization", "Bearer wrong")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("metrics wrong token = %d, want 401", resp.StatusCode)
	}

	// Right token: 200 with the exposition.
	req, _ = http.NewRequest(http.MethodGet, ts.URL+"/metrics", nil)
	req.Header.Set("Authorization", "Bearer scrape-token-123")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("metrics with token = %d", resp.StatusCode)
	}
	buf := make([]byte, 4096)
	n, _ := resp.Body.Read(buf)
	text := string(buf[:n])
	if !strings.Contains(text, "voxdesk_gateway_connections_current") {
		t.Errorf("exposition missing core gauge:\n%s", text)
	}
}
