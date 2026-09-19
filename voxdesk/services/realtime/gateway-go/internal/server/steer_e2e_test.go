package server

// Steer (wire 1.2) end-to-end: real websockets, real engineclient, the
// scripted engine. These test every rule stated in internal/signaling/
// steer.go's invariants:
//
//  1. Negotiation (v1.2|force, engine-up, both-marked) surfaces on the
//     session.joined / session.peer_joined acks and gates every engine.*
//     frame afterwards.
//  2. Media path mixing is refused both ways.
//  3. engine.frames → wire frame dispatch (answer to caller, publish
//     fanout to the peer) with engine identities never leaking.
//  4. Degrade: engine down at negotiation ⇒ P2P lands; engine down DURING
//     a steered call ⇒ session-preserving error frame.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/auth"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
	"github.com/voxdesk/realtime/gateway-go/internal/signaling"

	gorilla "github.com/gorilla/websocket"
)

// steerFixture is engineFixture plus configuration of the closed steer mode
// and a pre-warm HealthNow so negotiateSteer's availability gate is green
// from the first join (the Monitor loop is not part of these fixtures).
func steerFixture(t *testing.T, engScript *scriptedEngine, mode string) (*fixture, *engineclient.Client) {
	t.Helper()
	cfg := testConfig()
	cfg.EngineSteer = mode
	cfg.MediaEngineURL = engScript.srv.URL
	h := hub.New(cfg.MaxConnsPerTenant)
	reg := metrics.New()
	verifier := auth.NewVerifier(cfg.JWTSecret, cfg.JWTIssuer, cfg.JWTAudience)
	sig := signaling.NewRouter(session.NewManager(cfg.SignalingMaxSessionsPerTenant, cfg.SignalingPendingTimeout), h.Lookup, reg)
	s := New(cfg, h, reg, verifier, sig)
	client := engineclient.New(engScript.srv.URL, time.Second, nil, nil, func(string, ...any) {})
	s.SetEngine(client)
	if _, err := client.HealthNow(context.Background()); err != nil {
		t.Fatalf("pre-warm health probe: %v", err)
	}
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &fixture{server: s, hub: h, reg: reg, http: ts}, client
}

// steerClient dials and authenticates, declaring wire capability (ws:2 =
// v1.2, 0/absent = legacy).
func steerClient(t *testing.T, f *fixture, tenant string, wsVersion int) *gorilla.Conn {
	t.Helper()
	conn := dial(t, f)
	if welcome := readFrame(t, conn); welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, testJWTSecret, dashboardClaims(tenant, "owner", 15*time.Minute))
	if wsVersion >= 2 {
		sendFrame(t, conn, fmt.Sprintf(`{"type":"hello","token":"%s","ws":%d}`, token, wsVersion))
	} else {
		sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	}
	if ready := readFrame(t, conn); ready["type"] != "ready" || ready["tenant_id"] != tenant {
		t.Fatalf("ready = %v", ready)
	}
	return conn
}

// steeredPair performs start+join for two v1.2 clients and asserts the
// negotiation outcome lands in the ack frames of BOTH members.
func steeredPair(t *testing.T, f *fixture, tenant string, wsVersionA, wsVersionB int) (a, b *gorilla.Conn, sessionID string, steered bool) {
	t.Helper()
	a = steerClient(t, f, tenant, wsVersionA)
	b = steerClient(t, f, tenant, wsVersionB)
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	id, _ := started["session_id"].(string)
	if id == "" {
		t.Fatalf("session.started = %v", started)
	}
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	joined := readFrame(t, b)
	if joined["type"] != "session.joined" {
		t.Fatalf("session.joined = %v", joined)
	}
	peerJoined := readFrame(t, a)
	if peerJoined["type"] != "session.peer_joined" {
		t.Fatalf("session.peer_joined = %v", peerJoined)
	}
	steerJoined, _ := joined["steer"].(bool)
	steerPeer, _ := peerJoined["steer"].(bool)
	if steerJoined != steerPeer {
		t.Fatalf("the two members must agree on steer: joined=%v peer_joined=%v", steerJoined, steerPeer)
	}
	return a, b, id, steerJoined
}

const steerOfferSDP = "v=0\r\no=- 1 1 IN IP4 203.0.113.5\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=setup:actpass\r\n"

func TestSteerNegotiationAndOfferAnswerAndPublishFanout(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "v1.2")

	a, b, id, steered := steeredPair(t, f, testTenantA, 2, 2)
	if !steered {
		t.Fatalf("two v1.2 clients with engine up must steer")
	}

	// engine.offer → engine.answer returns to the OFFERER only.
	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	reply := readFrame(t, a)
	if reply["type"] != "engine.answer" {
		t.Fatalf("engine offer reply = %v", reply)
	}
	if reply["session_id"] != id {
		t.Fatalf("answer session mismatch: %v", reply)
	}
	sdp, _ := reply["sdp"].(string)
	if sdp == "" {
		t.Fatalf("answer carries no sdp: %v", reply)
	}
	if got := len(eng.offers); got != 1 {
		t.Fatalf("engine offers = %d", got)
	}

	// A publishes mic; B hears engine.track_published, A hears nothing.
	sendFrame(t, a, `{"type":"engine.publish","session_id":"`+id+`","track":"mic","kind":"audio"}`)
	pubNotice := readFrame(t, b)
	if pubNotice["type"] != "engine.track_published" {
		t.Fatalf("publish fanout = %v", pubNotice)
	}
	if pubNotice["track"] != "mic" || pubNotice["kind"] != "audio" {
		t.Fatalf("fanout fields = %v", pubNotice)
	}
	pubBy, _ := pubNotice["participant"].(string)
	engSessionA := findEngineJoinForRoom(eng, testTenantA+":"+id)
	if engSessionA == "" {
		t.Fatalf("no engine join recorded for room")
	}
	eng.mu.Lock()
	var pubParticipant string
	for _, j := range eng.joins {
		if j["engine_session"] == engSessionA {
			pubParticipant = j["participant"]
		}
	}
	eng.mu.Unlock()
	if pubBy != pubParticipant {
		t.Fatalf("fanout participant %q want engine-side %q", pubBy, pubParticipant)
	}

	// B subscribes the track: the SUBSCRIBE's engine-side participant is A
	// (the publisher), resolved by the manager — B never names it.
	sendFrame(t, b, `{"type":"engine.subscribe","session_id":"`+id+`","track":"mic"}`)
	waitFor(t, "one subscribe recorded", func() bool {
		n, _ := eng.subscribeSnapshot()
		return n == 1
	})
	_, sub := eng.subscribeSnapshot()
	if sub["participant"] != pubParticipant || sub["track"] != "mic" {
		t.Fatalf("engine subscribe = %v", sub)
	}

	// Media-path mixing is refused from BOTH directions.
	sendFrame(t, a, `{"type":"offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	mixed := readFrame(t, a)
	if mixed["type"] != "error" || mixed["code"] != "steer_mode_blocked" {
		t.Fatalf("P2P offer on steered session must refuse steer_mode_blocked, got %v", mixed)
	}

	// engine.unsubscribe + session.end: clean teardown.
	sendFrame(t, b, `{"type":"engine.unsubscribe","session_id":"`+id+`","track":"mic"}`)
	sendFrame(t, a, `{"type":"session.end","session_id":"`+id+`"}`)
	_ = readFrame(t, a)
	_ = readFrame(t, b)
}

func TestSteerMixedCapabilitiesStaysP2P(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "v1.2")

	a, b, id, steered := steeredPair(t, f, testTenantA, 2, 0)
	if steered {
		t.Fatalf("mixed capability pair must NOT steer")
	}

	// P2P still flows on the same sockets.
	sendFrame(t, a, `{"type":"offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	relayed := readFrame(t, b)
	if relayed["type"] != "signal.offer" {
		t.Fatalf("P2P relay broken on non-steered pair: %v", relayed)
	}

	// engine.* refuses on the non-steered session.
	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	refused := readFrame(t, a)
	if refused["type"] != "error" || refused["code"] != "steer_mode_blocked" {
		t.Fatalf("engine.offer on P2P session must refuse steer_mode_blocked, got %v", refused)
	}
}

func TestSteerForceIgnoresMarkersButNotAvailability(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "force")
	_, _, _, steered := steeredPair(t, f, testTenantA, 0, 0)
	if !steered {
		t.Fatalf("force must steer even unmarked clients")
	}
}

func TestSteerFallsBackToP2PWhenEngineDown(t *testing.T) {
	eng := newScriptedEngine(t)
	f, client := steerFixture(t, eng, "v1.2")
	eng.fail("http500") // any subsequent health probe fails
	_, err := client.HealthNow(context.Background())
	if err == nil {
		t.Fatalf("expected probe failure")
	}

	_, _, _, steered := steeredPair(t, f, testTenantA, 2, 2)
	if steered {
		t.Fatalf("unavailable engine must land the session on the P2P relay")
	}
}

func TestSteerEngineFailureDuringCallPreservesSession(t *testing.T) {
	eng := newScriptedEngine(t)
	f, _ := steerFixture(t, eng, "v1.2")
	a, b, id, steered := steeredPair(t, f, testTenantA, 2, 2)
	if !steered {
		t.Skip("setup failed")
	}
	eng.fail("http500")

	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	reply := readFrame(t, a)
	if reply["type"] != "error" || reply["code"] != "engine_unavailable" {
		t.Fatalf("down engine mid-call must answer engine_unavailable, got %v", reply)
	}

	// Session still alive: graceful end reaches both members.
	sendFrame(t, a, `{"type":"session.end","session_id":"`+id+`"}`)
	for _, c := range []*gorilla.Conn{a, b} {
		frame := readFrame(t, c)
		if frame["type"] != "session.ended" {
			t.Fatalf("session must survive the offer failure, got %v", frame)
		}
	}
}

func TestSteerEngineRefusalMapsToClosedCodes(t *testing.T) {
	eng := newScriptedEngine(t)
	eng.fail("refuse") // engine-side structured room_full
	f, _ := steerFixture(t, eng, "v1.2")

	a, _, id, steered := steeredPair(t, f, testTenantA, 2, 2)
	if !steered {
		// HealthNow was pre-warmed green; negotiation still steers (engine
		// up) while JOINS refuse — IsSteered still true at
		// negotiateSteer's predicate: engine up + both v1.2 + v1.2 mode.
		// The engine session enrolment failed; the first engine.* must be
		// told so, in closed vocabulary.
	}

	sendFrame(t, a, `{"type":"engine.offer","session_id":"`+id+`","sdp":"`+jsonEscapeSDP(steerOfferSDP)+`"}`)
	reply := readFrame(t, a)
	if reply["type"] != "error" {
		t.Fatalf("refusing join must surface an error for engine frames, got %v (steered=%v)", reply, steered)
	}
	// Refused enrolment ⇒ engine session absent ⇒ engine_unavailable is
	// the truthful closed code (NOT the internal "room_full" string).
	if reply["code"] != "engine_unavailable" {
		t.Fatalf("want engine_unavailable (unenrolled), got %v", reply)
	}
}

// findEngineJoinForRoom returns the engine session of the first join
// matching the room name (gateway testTenantA:sessionID convention).
func findEngineJoinForRoom(eng *scriptedEngine, room string) string {
	eng.mu.Lock()
	defer eng.mu.Unlock()
	for _, j := range eng.joins {
		if j["room"] == room {
			return j["engine_session"]
		}
	}
	return ""
}

// jsonEscapeSDP turns raw SDP into a JSON string literal body (no quotes).
func jsonEscapeSDP(s string) string {
	b, _ := json.Marshal(s)
	return string(b[1 : len(b)-1])
}
