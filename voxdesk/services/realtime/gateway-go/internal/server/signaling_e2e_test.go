package server

import (
	"testing"
	"time"

	gorilla "github.com/gorilla/websocket"
)

// Signaling over real sockets: two authenticated connections of one tenant
// chat through the full upgrade → hello → session → relay → teardown path.
// This is the wire-level proof that the session/signaling/protocol layers a
// browser would touch are actually assembled — everything above this file
// tests the layers in isolation.

// signalingClient dials and authenticates as one tenant, consuming the
// welcome and ready frames, so the tests start at "ready".
func signalingClient(t *testing.T, f *fixture, tenant string) *gorilla.Conn {
	t.Helper()
	conn := dial(t, f)
	if welcome := readFrame(t, conn); welcome["type"] != "welcome" {
		t.Fatalf("welcome = %v", welcome)
	}
	token := mintToken(t, testJWTSecret, dashboardClaims(tenant, "owner", 15*time.Minute))
	sendFrame(t, conn, `{"type":"hello","token":"`+token+`"}`)
	if ready := readFrame(t, conn); ready["type"] != "ready" || ready["tenant_id"] != tenant {
		t.Fatalf("ready = %v", ready)
	}
	return conn
}

func TestSignalingRoundTripOverRealSockets(t *testing.T) {
	f := newFixture(t)
	a := signalingClient(t, f, testTenantA)
	b := signalingClient(t, f, testTenantA)

	// A starts; the ack doubles as the join capability.
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	if started["type"] != "session.started" || started["role"] != "initiator" {
		t.Fatalf("session.started = %v", started)
	}
	id, _ := started["session_id"].(string)
	if id == "" {
		t.Fatalf("session.started without an id: %v", started)
	}

	// B joins by id; the initiator hears about the arrival.
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	joined := readFrame(t, b)
	if joined["type"] != "session.joined" || joined["role"] != "responder" {
		t.Fatalf("session.joined = %v", joined)
	}
	peerJoined := readFrame(t, a)
	if peerJoined["type"] != "session.peer_joined" || peerJoined["peer_role"] != "responder" {
		t.Fatalf("session.peer_joined = %v", peerJoined)
	}

	// Offer A→B, byte-verbatim; answer B→A.
	offer := "v=0 o=- 1 1 IN IP4 10.0.0.1 m=audio 9 UDP/TLS/RTP/SAVPF 111"
	sendFrame(t, a, `{"type":"offer","session_id":"`+id+`","sdp":"`+offer+`"}`)
	gotOffer := readFrame(t, b)
	if gotOffer["type"] != "signal.offer" || gotOffer["sdp"] != offer {
		t.Fatalf("relayed offer = %v", gotOffer)
	}
	sendFrame(t, b, `{"type":"answer","session_id":"`+id+`","sdp":"`+offer+`"}`)
	gotAnswer := readFrame(t, a)
	if gotAnswer["type"] != "signal.answer" || gotAnswer["sdp"] != offer {
		t.Fatalf("relayed answer = %v", gotAnswer)
	}

	// Trickle including the end-of-candidates null.
	sendFrame(t, a, `{"type":"candidate","session_id":"`+id+`","candidate":{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}}`)
	gotCand := readFrame(t, b)
	if gotCand["type"] != "signal.candidate" {
		t.Fatalf("relayed candidate type = %v", gotCand["type"])
	}
	candBody, ok := gotCand["candidate"].(map[string]any)
	if !ok || candBody["sdpMid"] != "0" {
		t.Fatalf("relayed candidate body = %v", gotCand["candidate"])
	}
	sendFrame(t, b, `{"type":"candidate","session_id":"`+id+`","candidate":null}`)
	if eoc := readFrame(t, a); eoc["candidate"] != nil {
		// JSON null decodes to nil inside the generic map.
		t.Fatalf("end-of-candidates relayed as %v", eoc["candidate"])
	}

	// Member-initiated end: requester and peer both hear the reason.
	sendFrame(t, b, `{"type":"session.end","session_id":"`+id+`"}`)
	for name, conn := range map[string]*gorilla.Conn{"requester": b, "peer": a} {
		end := readFrame(t, conn)
		if end["type"] != "session.ended" || end["reason"] != "member_ended" {
			t.Fatalf("%s session.ended = %v", name, end)
		}
	}

	// Disconnect-ending is the interesting half of the lifecycle: A starts a
	// second session, B joins, then B's socket simply DIES — and A learns
	// peer_disconnected without anyone sending session.end.
	sendFrame(t, a, `{"type":"session.start"}`)
	started2 := readFrame(t, a)
	id2, _ := started2["session_id"].(string)
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id2+`"}`)
	_ = readFrame(t, b) // B's own join ack; irrelevant next
	_ = readFrame(t, a) // A's peer_joined
	_ = b.Close()
	end := readFrame(t, a)
	if end["type"] != "session.ended" || end["reason"] != "peer_disconnected" {
		t.Fatalf("disconnect session.ended = %v", end)
	}
}

func TestSignalingJoinAcrossTenantsIsCollapsedUnknown(t *testing.T) {
	f := newFixture(t)
	a := signalingClient(t, f, testTenantA)
	x := signalingClient(t, f, testTenantB)

	sendFrame(t, a, `{"type":"session.start"}`)
	id, _ := readFrame(t, a)["session_id"].(string)

	// The id exists — but under another tenant, so the refusal is
	// indistinguishable from a made-up id. tenant=A's session also survives
	// untouched (the probe must not corrupt it).
	sendFrame(t, x, `{"type":"session.join","session_id":"`+id+`"}`)
	errFrame := readFrame(t, x)
	if errFrame["type"] != "error" || errFrame["code"] != "session_unknown" {
		t.Fatalf("cross-tenant join = %v", errFrame)
	}
	x2 := signalingClient(t, f, testTenantA)
	sendFrame(t, x2, `{"type":"session.join","session_id":"`+id+`"}`)
	if joined := readFrame(t, x2); joined["type"] != "session.joined" {
		t.Fatalf("the session was corrupted by the probe: %v", joined)
	}
}

func TestSignalingFramesBeforeHelloAreRefused(t *testing.T) {
	f := newFixture(t)
	conn := dial(t, f)
	_ = readFrame(t, conn) // welcome

	sendFrame(t, conn, `{"type":"session.start"}`)
	errFrame := readFrame(t, conn)
	if errFrame["type"] != "error" || errFrame["code"] != "hello_required" {
		t.Fatalf("pre-auth session.start = %v", errFrame)
	}
	sendFrame(t, conn, `{"type":"offer","session_id":"s","sdp":"v=0"}`)
	errFrame = readFrame(t, conn)
	if errFrame["code"] != "hello_required" {
		t.Fatalf("pre-auth offer = %v", errFrame)
	}
}

func TestSignalingNoticesCoexistWithTheNoticePlane(t *testing.T) {
	// One socket driving BOTH planes: subscriptions and a signaling session
	// on the same connection must not starve or confuse one another — that
	// is the dashboard's actual shape (a page that watches calls AND can
	// start a WebRTC monitor session).
	f := newFixture(t)
	a := signalingClient(t, f, testTenantA)
	b := signalingClient(t, f, testTenantA)

	sendFrame(t, a, `{"type":"subscribe","room":"calls"}`)
	if sub := readFrame(t, a); sub["type"] != "subscribed" {
		t.Fatalf("subscribed = %v", sub)
	}
	sendFrame(t, a, `{"type":"session.start"}`)
	started := readFrame(t, a)
	id, _ := started["session_id"].(string)
	sendFrame(t, b, `{"type":"session.join","session_id":"`+id+`"}`)
	_ = readFrame(t, b)
	_ = readFrame(t, a) // peer_joined

	// An ingest delivery and an offer interleave on A's socket; both
	// arrive, in order (subscribe happened first → its subscription is the
	// standing one).
	sendFrame(t, b, `{"type":"session.start"}`) // B moves to a second session? No — refused.
	if errFrame := readFrame(t, b); errFrame["code"] != "already_in_session" {
		t.Fatalf("second session on one socket = %v", errFrame)
	}
	status, _ := f.post(t,
		`{"tenant_id":"`+testTenantA+`","room":"calls","kind":"call.updated","payload":{"x":1},"event_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}`,
		"Bearer "+testIngestSecret)
	if status != 200 {
		t.Fatalf("ingest while signaling: %d", status)
	}
	sendFrame(t, b, `{"type":"offer","session_id":"`+id+`","sdp":"v=0 o=- 1"}`)

	first := readFrame(t, a)
	second := readFrame(t, a)
	got := map[string]bool{first["type"].(string): true, second["type"].(string): true}
	if !got["delivery"] || !got["signal.offer"] {
		t.Fatalf("expected delivery + signal.offer interleaved, got %v, %v", first["type"], second["type"])
	}
}
