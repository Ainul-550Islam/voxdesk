package server

import (
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/broker/brokertest"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

// newRedisFixture wires a full gateway configured for the redis broker.
// Two fixtures against one fake redis = a two-replica deployment.
func newRedisFixture(t *testing.T, redisURL string) *fixture {
	t.Helper()
	cfg := testConfig()
	cfg.BrokerKind = "redis"
	cfg.RedisURL = redisURL
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	t.Cleanup(s.CloseAll)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}
}

// TestMultiReplicaIngestOverRedisBus is the multi-node proof of the whole
// phase: an ingest POSTed to replica A is delivered to a browser connected
// to replica B, byte-verbatim, exactly once, while A's HTTP response
// reports only A's (empty) local fan-out — the response counters stay
// per-replica-exact, never aggregated into fiction.
func TestMultiReplicaIngestOverRedisBus(t *testing.T) {
	t.Parallel()

	fake, err := brokertest.Start()
	if err != nil {
		t.Fatalf("start fake redis: %v", err)
	}
	t.Cleanup(fake.Stop)

	fA := newRedisFixture(t, fake.URL())
	fB := newRedisFixture(t, fake.URL())

	// Both buses subscribed before any publish (Subscribe went out when
	// each server's New ran, i.e. before its bus first connected — the
	// reconnect path's re-subscribe is the same code path).
	if !fake.WaitForSubscribers(ingestTopic, 2, 5*time.Second) {
		t.Fatalf("both replicas must hold bus subscriptions, have %d", fake.SubscriberCount(ingestTopic))
	}

	// A browser session lands on REPLICA B: welcome → hello → ready →
	// subscribe → subscribed.
	conn := dial(t, fB)
	welcome := readFrame(t, conn)
	if welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "admin", 5*time.Minute))
	sendFrame(t, conn, fmt.Sprintf(`{"type":"hello","token":%q}`, token))
	ready := readFrame(t, conn)
	if ready["type"] != "ready" || ready["tenant_id"] != testTenantA {
		t.Fatalf("ready = %v", ready)
	}
	sendFrame(t, conn, `{"type":"subscribe","room":"calls"}`)
	subscribed := readFrame(t, conn)
	if subscribed["type"] != "subscribed" || subscribed["room"] != "calls" {
		t.Fatalf("subscribed = %v", subscribed)
	}

	// The API publishes to REPLICA A — the replica the browser is NOT on.
	// Payload carries nested shape to prove byte-verbatim forwarding over a
	// JSON-inside-JSON bus hop.
	payload := `{"call_id":"call-42","levels":[1,2,3],"meta":{"origin":"api","ok":true}}`
	status, resp := fA.post(t, fmt.Sprintf(`{"tenant_id":%q,"room":"calls","kind":"call.updated","payload":%s,"event_id":"33333333-3333-3333-3333-333333333333"}`, testTenantA, payload), "Bearer "+testIngestSecret)
	if status != 200 {
		t.Fatalf("ingest status = %d (%v)", status, resp)
	}
	// A has NO local subscribers to (tenantA, calls): its response reports
	// its own empty fan-out. The "0 delivered" here is the CONTRACT — per-
	// replica accounting, cross-node visibility not faked into one number.
	if resp["delivered"] != 0.0 || resp["dropped"] != 0.0 {
		t.Fatalf("A's response must report A's local fan-out only: %v", resp)
	}

	// …and the browser on B hears it exactly once, byte-verbatim.
	delivery := readFrame(t, conn)
	if delivery["type"] != "delivery" || delivery["room"] != "calls" || delivery["kind"] != "call.updated" {
		t.Fatalf("delivery envelope wrong: %v", delivery)
	}
	if delivery["event_id"] != "33333333-3333-3333-3333-333333333333" {
		t.Fatalf("event_id must survive the bus hop: %v", delivery["event_id"])
	}
	gotPayload, _ := delivery["payload"].(map[string]any)
	if gotPayload["call_id"] != "call-42" {
		t.Fatalf("payload corrupted over the bus: %v", gotPayload)
	}
	levels, _ := gotPayload["levels"].([]any)
	if len(levels) != 3 || levels[2] != 3.0 {
		t.Fatalf("nested array must arrive untouched: %v", levels)
	}
	meta, _ := gotPayload["meta"].(map[string]any)
	if meta["origin"] != "api" || meta["ok"] != true {
		t.Fatalf("nested object must arrive untouched: %v", meta)
	}

	// No double delivery: A's own bus echo must have been suppressed, and
	// B's local hub is the only source of the frame.
	_ = conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	if _, extra, err := conn.ReadMessage(); err == nil {
		t.Fatalf("second frame %q: the event must be delivered exactly once", extra)
	}

	// B counted its remote-hop delivery even though B never saw an HTTP
	// POST (accounting for bus-served fan-out lives at the delivery point).
	rendered := fB.reg.Render(0, 0)
	if !strings.Contains(rendered, "voxdesk_gateway_deliveries_total 1") {
		t.Fatalf("B must count the remote delivery locally:\n%s", rendered)
	}
}

// TestBusOutageDegradesToLocalOnly pins the failure mode a deployment
// actually hits: redis dies, ingest POSTs stay 200 with exact local
// counters, already-connected browsers on the SAME replica keep receiving.
func TestBusOutageDegradesToLocalOnly(t *testing.T) {
	t.Parallel()

	fake, err := brokertest.Start()
	if err != nil {
		t.Fatalf("start fake redis: %v", err)
	}
	t.Cleanup(fake.Stop)

	f := newRedisFixture(t, fake.URL())
	if !fake.WaitForSubscribers(ingestTopic, 1, 5*time.Second) {
		t.Fatal("replica must hold a bus subscription before the outage")
	}

	fake.Stop() // the bus is gone

	// Same-replica browser.
	conn := dial(t, f)
	_ = readFrame(t, conn)
	token := mintToken(t, testJWTSecret, dashboardClaims(testTenantA, "agent", 5*time.Minute))
	sendFrame(t, conn, fmt.Sprintf(`{"type":"hello","token":%q}`, token))
	if ready := readFrame(t, conn); ready["type"] != "ready" {
		t.Fatalf("ready = %v", ready)
	}
	sendFrame(t, conn, `{"type":"subscribe","room":"metrics"}`)
	if sub := readFrame(t, conn); sub["type"] != "subscribed" {
		t.Fatalf("subscribed = %v", sub)
	}

	// POST mid-outage: 200, this replica's delivery exact (1 to the local
	// browser), cross-node hop dropped and the drop accounted on the bus.
	status, resp := f.post(t, fmt.Sprintf(`{"tenant_id":%q,"room":"metrics","kind":"metrics.tick","payload":{"cpu":0.5}}`, testTenantA), "Bearer "+testIngestSecret)
	if status != 200 || resp["delivered"] != 1.0 {
		t.Fatalf("outage ingest must stay exact-locally: status=%d resp=%v", status, resp)
	}
	delivery := readFrame(t, conn)
	if delivery["kind"] != "metrics.tick" {
		t.Fatalf("local delivery during outage wrong: %v", delivery)
	}
	rb, ok := f.server.Broker().(interface{ BusDrops() int64 })
	if !ok {
		t.Fatalf("redis-mode broker must expose outage accounting, got %T", f.server.Broker())
	}
	deadline := time.Now().Add(2 * time.Second)
	for rb.BusDrops() < 1 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if rb.BusDrops() < 1 {
		t.Fatal("the dropped bus hop must be counted, not silent")
	}
}
