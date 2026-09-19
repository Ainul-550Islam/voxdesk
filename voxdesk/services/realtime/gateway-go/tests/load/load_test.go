//go:build load

// Package load holds the gateway's throughput/latency harness. It is behind
// the `load` build tag BY DESIGN: it opens hundreds of sockets and measures
// wall-clock fan-out, so it must never run as part of the deterministic
// correctness gate. Run it on purpose:
//
//	go test -tags load ./tests/load/ -run . -v -timeout 120s
//
// This is a SMOKE-level load check (does fan-out scale to a few hundred
// connections with reasonable latency?), not a capacity study — that wants
// k6/Locust against a deployed node, recorded in ops runbooks.
package load

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
	"sync/atomic"
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/config"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/server"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

const (
	jwtSecret    = "0123456789abcdef0123456789abcdef"
	ingestSecret = "ingest-shared-secret-0123"
	tenant       = "11111111-1111-1111-1111-111111111111"
)

func mintToken(t *testing.T, jti string) string {
	t.Helper()
	now := time.Now()
	header, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	body, _ := json.Marshal(map[string]any{
		"sub": "99999999-8888-7777-6666-555555555555", "tid": tenant, "role": "admin",
		"tv": float64(1), "typ": "access", "iat": float64(now.Unix()), "nbf": float64(now.Unix()),
		"exp": float64(now.Add(10 * time.Minute).Unix()), "iss": "voxdesk", "aud": "voxdesk-api", "jti": jti,
	})
	head := base64.RawURLEncoding.EncodeToString(header)
	payload := base64.RawURLEncoding.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(jwtSecret))
	mac.Write([]byte(head + "." + payload))
	return head + "." + payload + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

// TestFanOutSmoke opens 200 subscribers (well under the fixture's 10k cap,
// representative of a busy wallboard floor), publishes 50 events, and
// asserts every subscriber hears all of them, with the LAST delivery
// landing inside a generous deadline — the "hub doesn't fall over under
// realistic burst" property.
func TestFanOutSmoke(t *testing.T) {
	const conns, publishes = 200, 50

	cfg := config.Config{
		JWTSecret: jwtSecret, JWTIssuer: "voxdesk", JWTAudience: "voxdesk-api",
		IngestSecret: ingestSecret, MaxConnections: 10_000, MaxConnsPerTenant: 1_000,
		MaxSubscriptionsPerConn: 8, OutgoingBuffer: 256,
		MaxMessageBytes: 16 * 1024, MaxIngestPayloadBytes: 4 * 1024,
		WriteWait: 2 * time.Second, AuthTimeout: 5 * time.Second,
		PingInterval: 10 * time.Second, PongTimeout: 30 * time.Second,
		ShutdownTimeout:      5 * time.Second,
		MessageRatePerSecond: 1_000, MessageBurst: 1_000,
		IdempotencyTTL: time.Minute, IdempotencyCapacity: 1_000,
		SignalingMaxSessionsPerTenant: 8, SignalingPendingTimeout: time.Minute,
	}
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sessions := session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout)
	signaler := signaling.NewRouter(sessions, h.Lookup, reg)
	srv := server.New(cfg, h, reg, verifier, signaler)
	httpSrv := httptest.NewServer(srv.Handler())
	t.Cleanup(httpSrv.Close)
	t.Cleanup(srv.CloseAll)

	wsURL := "ws" + strings.TrimPrefix(httpSrv.URL, "http") + "/ws"
	var received atomic.Int64
	var wg sync.WaitGroup
	clients := make([]*gorilla.Conn, 0, conns)

	start := time.Now()
	for i := 0; i < conns; i++ {
		conn, _, err := gorilla.DefaultDialer.Dial(wsURL, nil)
		if err != nil {
			t.Fatalf("dial %d: %v", i, err)
		}
		clients = append(clients, conn)
		defer conn.Close()
		// welcome, hello, ready, subscribe, subscribed.
		if _, _, err := conn.ReadMessage(); err != nil {
			t.Fatalf("welcome %d: %v", i, err)
		}
		hello := fmt.Sprintf(`{"type":"hello","token":%q}`, mintToken(t, fmt.Sprintf("load-%d", i)))
		if err := conn.WriteMessage(gorilla.TextMessage, []byte(hello)); err != nil {
			t.Fatalf("hello %d: %v", i, err)
		}
		if _, _, err := conn.ReadMessage(); err != nil {
			t.Fatalf("ready %d: %v", i, err)
		}
		if err := conn.WriteMessage(gorilla.TextMessage, []byte(`{"type":"subscribe","room":"calls"}`)); err != nil {
			t.Fatalf("subscribe %d: %v", i, err)
		}
		if _, _, err := conn.ReadMessage(); err != nil {
			t.Fatalf("subscribed %d: %v", i, err)
		}

		wg.Add(1)
		go func(c *gorilla.Conn) {
			defer wg.Done()
			for {
				if err := c.SetReadDeadline(time.Now().Add(30 * time.Second)); err != nil {
					return
				}
				_, _, err := c.ReadMessage()
				if err != nil {
					return
				}
				received.Add(1)
			}
		}(conn)
	}
	t.Logf("%d sessions established in %v", conns, time.Since(start))

	start = time.Now()
	for n := 0; n < publishes; n++ {
		body := fmt.Sprintf(`{"tenant_id":%q,"room":"calls","kind":"call.updated","payload":{"seq":%d}}`, tenant, n)
		req, err := http.NewRequest(http.MethodPost, httpSrv.URL+"/ingest/v1/publish", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+ingestSecret)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("publish %d: %v", n, err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatalf("publish %d status %d", n, resp.StatusCode)
		}
	}

	want := int64(conns * publishes)
	deadline := time.Now().Add(30 * time.Second)
	for received.Load() < want && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	elapsed := time.Since(start)
	if got := received.Load(); got != want {
		t.Fatalf("delivered %d of %d frames", got, want)
	}
	t.Logf("fanned %d events to %d conns (%d frames) in %v — %.0f frames/s",
		publishes, conns, want, elapsed, float64(want)/elapsed.Seconds())

	rendered := reg.Render(0, 0)
	if !strings.Contains(rendered, "voxdesk_gateway_dropped_total 0") {
		t.Fatalf("256-deep buffers must absorb the burst without drops:\n%s", rendered)
	}
	_ = clients
}
