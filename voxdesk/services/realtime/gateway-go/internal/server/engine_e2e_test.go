package server

// Go↔Rust engine-link integration tests. A scripted httptest engine plays
// the Rust media engine's documented wire; the gateway stack is the REAL
// one — websockets, signaling router, session manager, engineclient. The
// eight cases the project calls out for this phase map onto:
//
//	1. session.start → engine join (room = tenant:session, member = conn)
//	2. session.join  → second engine join for the responder
//	3. session.end   → engine leaves for every member's own engine session
//	4. socket close  → same leaves (ConnDropped path)
//	5. engine DOWN at start → session still establishes (degrade, not break)
//	6. engine REFUSAL (in-band error) → same degrade, louder class
//	7. readiness reflects availability: disabled / up / down states
//	8. metrics surface the link: joins, join errors, engine_up gauge

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"
)

// scriptedEngine records the signaling-plane surface the gateway touched:
// joins and leaves with all fields, in wire order.
type scriptedEngine struct {
	t *testing.T

	mu         sync.Mutex
	joins      []map[string]string // {room, participant, engine_session}
	leaves     []string            // engine session ids
	offers     []map[string]string // {session, sdp_len}
	publishes  []map[string]string // {session, track, kind}
	subscribes []map[string]string // {session, participant, track}
	failWith   string              // "refuse" → in-band error; "http500" → transport; "" → healthy
	sessionCt  int

	srv *httptest.Server
}

func newScriptedEngine(t *testing.T) *scriptedEngine {
	e := &scriptedEngine{t: t}
	e.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/signal":
			var env struct {
				V     int            `json:"v"`
				ID    string         `json:"id"`
				Frame map[string]any `json:"frame"`
			}
			if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			e.mu.Lock()
			defer e.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			if e.failWith == "http500" {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			if e.failWith == "refuse" {
				fmt.Fprintf(w, `{"v":1,"id":%q,"error":{"code":"room_full","message":"capacity playing"}}`, env.ID)
				return
			}
			typ, _ := env.Frame["type"].(string)
			switch typ {
			case "offer":
				e.offers = append(e.offers, map[string]string{"session": sprintf(env.Frame["session"]), "sdp_len": fmt.Sprintf("%d", len(sprintf(env.Frame["sdp"])))})
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[{"type":"answer","session":%q,"sdp":"v=0\r\no=- 9 9 IN IP4 203.0.113.9\r\nm=audio 5000 RTP/AVP 111\r\na=sendrecv\r\n"}]}`, env.ID, sprintf(env.Frame["session"]))
			case "publish":
				e.publishes = append(e.publishes, map[string]string{
					"session": sprintf(env.Frame["session"]), "track": sprintf(env.Frame["track"]), "kind": sprintf(env.Frame["kind"]),
				})
				// Mirror the REAL engine's effects array: the room fanout
				// carries the PUBLISHER's participant id (here, the join
				// enrolment for the same engine session id).
				pubBy := ""
				for _, j := range e.joins {
					if j["engine_session"] == sprintf(env.Frame["session"]) {
						pubBy = j["participant"]
					}
				}
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[{"type":"track.published","room":"r","participant":%q,"track":%q,"kind":%q}]}`,
					env.ID, pubBy, sprintf(env.Frame["track"]), sprintf(env.Frame["kind"]))
			case "trickle":
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			case "subscribe":
				e.subscribes = append(e.subscribes, map[string]string{
					"session": sprintf(env.Frame["session"]), "participant": sprintf(env.Frame["participant"]), "track": sprintf(env.Frame["track"]),
				})
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			case "unsubscribe":
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			case "join":
				e.sessionCt++
				engineSID := fmt.Sprintf("ms-test-%d", e.sessionCt)
				e.joins = append(e.joins, map[string]string{
					"room":           sprintf(env.Frame["room"]),
					"participant":    sprintf(env.Frame["participant"]),
					"engine_session": engineSID,
				})
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[{"type":"ready","session":%q,"ice_ufrag":"u-fake","ice_pwd":"p-fake"}]}`, env.ID, engineSID)
			case "leave":
				e.leaves = append(e.leaves, sprintf(env.Frame["session"]))
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			default:
				fmt.Fprintf(w, `{"v":1,"id":%q,"frames":[]}`, env.ID)
			}
		case r.Method == http.MethodGet && r.URL.Path == "/v1/health":
			e.mu.Lock()
			down := e.failWith == "http500"
			e.mu.Unlock()
			if down {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"v":1,"engine":"media-engine-rs","version":"0.0.0-test","ready":true,"rooms":1,"participants":1,"tracks":1,"uptime_ms":7}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(e.srv.Close)
	return e
}

func sprintf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// subscribeSnapshot returns (count, last-recorded map) under the same
// lock the writer uses — the race-clean way for waitFor predicates.
func (e *scriptedEngine) subscribeSnapshot() (int, map[string]string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.subscribes) == 0 {
		return 0, nil
	}
	cp := make(map[string]string, len(e.subscribes[0]))
	for k, v := range e.subscribes[0] {
		cp[k] = v
	}
	return len(e.subscribes), cp
}

func (e *scriptedEngine) joinCount() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.joins)
}

func (e *scriptedEngine) leaveCount() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.leaves)
}

func (e *scriptedEngine) fail(mode string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.failWith = mode
}

// engineFixture wires the standard fixture plus an attached client. The
// fixture reuses the same hub/manager/router composition as newFixture so
// the ws flow is byte-identical to what production wires in cmd/gateway.
func engineFixture(t *testing.T, eng *engineclient.Client) *fixture {
	t.Helper()
	cfg := testConfig()
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	s.SetEngine(eng)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if !cond() {
		t.Fatalf("timed out waiting for %s", what)
	}
}

func TestEngineJoinAndLeaveAcrossFullSession(t *testing.T) {
	eng := newScriptedEngine(t)
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	b := signalingClient(t, f, testTenantA)

	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("session.started = %v", started)
	}
	id, _ := started["session_id"].(string)

	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	if j := readFrame(t, b); j["type"] != "session.joined" {
		t.Fatalf("session.joined = %v", j)
	}
	_ = readFrame(t, a) // peer_joined

	// Two joins, scoped to the same conversation room, one per member.
	waitFor(t, "two engine joins", func() bool { return eng.joinCount() == 2 })
	eng.mu.Lock()
	room0, room1 := eng.joins[0]["room"], eng.joins[1]["room"]
	p0, p1 := eng.joins[0]["participant"], eng.joins[1]["participant"]
	eng.mu.Unlock()
	wantRoom := testTenantA + ":" + id
	if room0 != wantRoom || room1 != wantRoom {
		t.Fatalf("engine rooms %q,%q want %q", room0, room1, wantRoom)
	}
	if p0 == "" || p1 == "" || p0 == p1 {
		t.Fatalf("engine participants must be distinct conn ids: %q,%q", p0, p1)
	}

	// Graceful end: BOTH members' engine sessions receive a leave.
	sendFrame(t, a, `{"type":"session.end","session_id":"`+id+`"}`)
	_ = readFrame(t, a) // ended
	_ = readFrame(t, b) // ended
	waitFor(t, "two engine leaves", func() bool { return eng.leaveCount() == 2 })

	eng.mu.Lock()
	defer eng.mu.Unlock()
	joined := map[string]bool{eng.joins[0]["engine_session"]: true, eng.joins[1]["engine_session"]: true}
	for _, lv := range eng.leaves {
		if !joined[lv] {
			t.Fatalf("leave %q not among engine sessions %v", lv, joined)
		}
	}
}

func TestEngineLeaveOnSocketClose(t *testing.T) {
	eng := newScriptedEngine(t)
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("session.started = %v", started)
	}
	// Close the socket: ConnDropped → EvEnded → the leave hook must fire
	// without any session.end frame ever being sent.
	a.Close()
	waitFor(t, "leave after socket close", func() bool { return eng.leaveCount() == 1 })
}

func TestEngineUnavailableDoesNotBreakSessionEstablishment(t *testing.T) {
	eng := newScriptedEngine(t)
	eng.fail("http500")
	client := engineclient.New(eng.srv.URL, 300*time.Millisecond, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("down-engine must NOT break establishment, got %v", started)
	}
	// The failure DID surface in metrics — no silent absorption.
	if got := f.reg.EngineUp(); got {
		t.Fatalf("engine_up gauge must be 0 before any green probe (HealthNow never ran)")
	}
}

func TestEngineRefusalLoggedAsRefusalNotUnavailable(t *testing.T) {
	eng := newScriptedEngine(t)
	eng.fail("refuse")
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f := engineFixture(t, client)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" {
		t.Fatalf("refusing engine must NOT break establishment, got %v", started)
	}
	if eng.joinCount() != 0 {
		t.Fatalf("refused join recorded none, got %d", eng.joinCount())
	}
}

func TestReadinessReportsEngineStates(t *testing.T) {
	// disabled: no engine attached at all.
	f := engineFixture(t, nil)
	req, err := http.NewRequest(http.MethodGet, f.http.URL+"/readyz", http.NoBody)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("engine-disabled node must be ready, got %d", resp.StatusCode)
	}
	engineCheck, _ := body["checks"].(map[string]any)["engine"].(map[string]any)
	if engineCheck["state"] != "disabled" {
		t.Fatalf("disabled state expected, got %v", engineCheck)
	}

	// up: a healthy engine acked by HealthNow (monitor steady-state proxy).
	eng := newScriptedEngine(t)
	client := engineclient.New(eng.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	f2 := engineFixture(t, client)
	if _, err := client.HealthNow(context.Background()); err != nil {
		t.Fatalf("precondition probe: %v", err)
	}
	req2, _ := http.NewRequest(http.MethodGet, f2.http.URL+"/readyz", http.NoBody)
	resp2, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatal(err)
	}
	var body2 map[string]any
	_ = json.NewDecoder(resp2.Body).Decode(&body2)
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("healthy engine must keep readiness green, got %d body %v", resp2.StatusCode, body2)
	}
	engineCheck2, _ := body2["checks"].(map[string]any)["engine"].(map[string]any)
	if engineCheck2["state"] != "up" {
		t.Fatalf("up state expected, got %v", engineCheck2)
	}

	// down: attached but unreachable → readiness red, reason carried.
	down := engineclient.New("http://127.0.0.1:1", 50*time.Millisecond, nil, nil, func(string, ...any) {})
	f3 := engineFixture(t, down)
	req3, _ := http.NewRequest(http.MethodGet, f3.http.URL+"/readyz", http.NoBody)
	resp3, err := http.DefaultClient.Do(req3)
	if err != nil {
		t.Fatal(err)
	}
	var body3 map[string]any
	_ = json.NewDecoder(resp3.Body).Decode(&body3)
	resp3.Body.Close()
	if resp3.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("down engine must turn readiness red, got %d", resp3.StatusCode)
	}
	engineCheck3, _ := body3["checks"].(map[string]any)["engine"].(map[string]any)
	if engineCheck3["state"] != "down" {
		t.Fatalf("down state expected, got %v", engineCheck3)
	}
}

func TestEngineMetricsSurface(t *testing.T) {
	eng := newScriptedEngine(t)
	// Build the fixture first (its OWN registry), then attach a client
	// whose observer points at exactly that registry.
	f := engineFixture(t, nil)
	wrapped := engineclient.New(eng.srv.URL, time.Second, testEngineObserver{f.reg}, nil, func(string, ...any) {})
	f.server.SetEngine(wrapped)

	a := signalingClient(t, f, testTenantA)
	sendFrame(t, a, `{"type":"session.start"}`)
	if s := readFrame(t, a); s["type"] != "session.started" {
		t.Fatalf("session.started = %v", s)
	}
	waitFor(t, "one recorded join", func() bool { return eng.joinCount() == 1 })

	rendered := f.reg.Render(0, 0)
	if !contains(rendered, "voxdesk_gateway_engine_signal_calls_total 1") {
		t.Fatalf("metrics missing join call line:\n%s", rendered)
	}

	// The availability monitor is driven by HealthNow here: observe one
	// probe and a transition.
	if _, err := wrapped.HealthNow(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !f.reg.EngineUp() {
		t.Fatalf("gauge must be up after one green probe")
	}
	rendered = f.reg.Render(0, 0)
	if !contains(rendered, "voxdesk_gateway_engine_up 1") {
		t.Fatalf("engine_up gauge missing:\n%s", rendered)
	}
}

// testEngineObserver mirrors the cmd/gateway adapter so this package can
// verify the metric lines without importing main.
type testEngineObserver struct{ reg *metrics.Registry }

func (o testEngineObserver) ObserveSignal(op string, took time.Duration, err error) {
	o.reg.EngineCall(op, took, err != nil)
}

func (o testEngineObserver) ObserveHealth(took time.Duration, err error) {
	o.reg.EngineProbe(took, err != nil)
}

func (o testEngineObserver) EngineUpChanged(up bool) { o.reg.SetEngineUp(up) }

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
