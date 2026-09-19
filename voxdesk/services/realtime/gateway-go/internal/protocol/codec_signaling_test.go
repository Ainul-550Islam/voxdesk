package protocol

import (
	"encoding/json"
	"strings"
	"testing"
)

// Signaling vocabulary, decoded with the same per-variant PRESENCE discipline
// the original four types get: an empty session_id and a missing one are the
// same refusal, and `{"candidate":null}` (end-of-candidates) is legal on
// purpose — that marker has to RELAY, not die at the decoder.

func TestDecodeSignalingVariants(t *testing.T) {
	cases := []struct {
		raw    string
		assert func(t *testing.T, msg *ClientMessage)
	}{
		{`{"type":"session.start"}`, func(t *testing.T, msg *ClientMessage) {
			if msg.Type != TypeSessionStart {
				t.Errorf("type = %q", msg.Type)
			}
		}},
		{`{"type":"session.join","session_id":"s-1"}`, func(t *testing.T, msg *ClientMessage) {
			if msg.SessionID != "s-1" {
				t.Errorf("session_id = %q", msg.SessionID)
			}
		}},
		{`{"type":"session.end","session_id":"s-1"}`, nil},
		{`{"type":"offer","session_id":"s-1","sdp":"v=0\r\no=- 1 1 IN IP4 10.0.0.1"}`, func(t *testing.T, msg *ClientMessage) {
			if !strings.HasPrefix(msg.SDP, "v=") {
				t.Errorf("sdp = %q", msg.SDP)
			}
		}},
		{`{"type":"answer","session_id":"s-1","sdp":"v=0"}`, nil},
		{`{"type":"candidate","session_id":"s-1","candidate":{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}}`, func(t *testing.T, msg *ClientMessage) {
			var cand map[string]any
			if err := json.Unmarshal(msg.Candidate, &cand); err != nil {
				t.Fatalf("candidate should be raw JSON: %v", err)
			}
			if cand["sdpMid"] != "0" {
				t.Errorf("candidate payload = %s", msg.Candidate)
			}
		}},
		{`{"type":"candidate","session_id":"s-1","candidate":null}`, nil}, // end-of-candidates
	}
	for i, tc := range cases {
		msg, err := DecodeClientMessage([]byte(tc.raw))
		if err != nil {
			t.Errorf("case %d (%s): %v", i, tc.raw, err)
			continue
		}
		if tc.assert != nil {
			tc.assert(t, msg)
		}
	}
}

func TestDecodeRejectsSignalingShapeViolations(t *testing.T) {
	for _, raw := range []string{
		`{"type":"session.join"}`,                      // no session id
		`{"type":"session.join","session_id":""}`,      // empty session id
		`{"type":"offer","session_id":"s-1"}`,          // sdp missing
		`{"type":"offer","session_id":"s-1","sdp":""}`, // sdp empty
		`{"type":"answer","session_id":"s-1"}`,         // sdp missing
		`{"type":"candidate","session_id":"s-1"}`,      // candidate missing (not null!)
	} {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Errorf("%s must be rejected at decode time", raw)
		}
	}
}

func TestIsValidSDP(t *testing.T) {
	for _, good := range []string{
		"v=0\r\no=- 1 1 IN IP4 10.0.0.1",
		"  v=0\n\nm=audio 9 UDP/TLS/RTP/SAVPF 111", // leading whitespace tolerated
	} {
		if !IsValidSDP(good) {
			t.Errorf("IsValidSDP(%q) = false, want true", good)
		}
	}
	for _, bad := range []string{"", "{}", "o=- 1 1 IN IP4 10.0.0.1", "v0=", "select * from calls"} {
		if IsValidSDP(bad) {
			t.Errorf("IsValidSDP(%q) = true, want false", bad)
		}
	}
}

func TestCandidateIsObject(t *testing.T) {
	// The two end-of-candidates forms relay verbatim.
	if !CandidateIsObject(json.RawMessage("null")) {
		t.Error("null (end-of-candidates) must pass and relay")
	}
	if !CandidateIsObject(json.RawMessage(`{"candidate":""}`)) {
		t.Error(`{"candidate":""} (end-of-candidates) must pass and relay`)
	}
	if !CandidateIsObject(json.RawMessage(`{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0","sdpMLineIndex":0}`)) {
		t.Error("a normal candidate object must pass")
	}
	if CandidateIsObject(json.RawMessage(`["not","an","object"]`)) {
		t.Error("an array is not a candidate")
	}
	if CandidateIsObject(json.RawMessage(strings.Repeat("x", MaxCandidateBytes+1))) {
		t.Error("over the relay cap must fail")
	}
	// Exactly-at-cap is legal: the boundary is off by design (cap prevents
	// abuse, not one byte more of legitimate ICE than yesterday).
	atCap := `{"candidate":"` + strings.Repeat("a", MaxCandidateBytes-len(`{"candidate":""}`)) + `"}`
	if !CandidateIsObject(json.RawMessage(atCap)) {
		t.Error("exactly-at-cap must pass")
	}
}

func TestSignalingServerFramesMarshalToWireShape(t *testing.T) {
	started := NewSessionStarted("sig-1")
	raw, _ := Marshal(started)
	for _, want := range []string{`"type":"session.started"`, `"session_id":"sig-1"`, `"role":"initiator"`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("session.started missing %s: %s", want, raw)
		}
	}

	joined := NewSessionJoined("sig-1", false)
	raw, _ = Marshal(joined)
	if !strings.Contains(string(raw), `"role":"responder"`) {
		t.Errorf("session.joined must pin the responder role: %s", raw)
	}

	peerJoined := NewSessionPeerJoined("sig-1", RoleResponder, false)
	raw, _ = Marshal(peerJoined)
	if !strings.Contains(string(raw), `"peer_role":"responder"`) {
		t.Errorf("session.peer_joined must say who arrived: %s", raw)
	}

	ended := NewSessionEnded("sig-1", "peer_disconnected")
	raw, _ = Marshal(ended)
	if !strings.Contains(string(raw), `"reason":"peer_disconnected"`) {
		t.Errorf("session.ended must carry the machine-readable reason: %s", raw)
	}

	offer := NewSignalOffer("sig-1", "v=0\r\no=- 1 1 IN IP4 10.0.0.1")
	raw, _ = Marshal(offer)
	if !strings.Contains(string(raw), `"type":"signal.offer"`) || !strings.Contains(string(raw), `"sdp":"v=0`) {
		t.Errorf("signal.offer must forward the body verbatim: %s", raw)
	}

	cand := NewSignalCandidate("sig-1", json.RawMessage(`{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host"}`))
	raw, _ = Marshal(cand)
	if !strings.Contains(string(raw), `"candidate":{"candidate":"candidate:1 1 udp`) {
		t.Errorf("signal.candidate must forward the object verbatim: %s", raw)
	}
}

// ---- wire 1.2 (steer) codec rules ----

func TestV12EngineFramesDecodeWithPresenceRules(t *testing.T) {
	must := func(body string) *ClientMessage {
		t.Helper()
		m, err := DecodeClientMessage([]byte(body))
		if err != nil {
			t.Fatalf("decode %s: %v", body, err)
		}
		return m
	}
	mustNot := func(body string) {
		t.Helper()
		if _, err := DecodeClientMessage([]byte(body)); err == nil {
			t.Fatalf("must refuse: %s", body)
		}
	}

	// happy paths
	if m := must(`{"type":"engine.offer","session_id":"s","sdp":"v=0"}`); m.SDP != "v=0" {
		t.Fatalf("offer = %+v", m)
	}
	if m := must(`{"type":"engine.candidate","session_id":"s","candidate":null}`); m == nil {
		t.Fatalf("null candidate (e.o.c) must decode")
	}
	if m := must(`{"type":"engine.publish","session_id":"s","track":"mic","kind":"audio"}`); m.Track != "mic" || m.Kind != "audio" {
		t.Fatalf("publish = %+v", m)
	}
	if m := must(`{"type":"engine.subscribe","session_id":"s","track":"mic"}`); m.Track != "mic" {
		t.Fatalf("subscribe = %+v", m)
	}
	if m := must(`{"type":"engine.unsubscribe","session_id":"s","track":"mic"}`); m.Track != "mic" {
		t.Fatalf("unsubscribe = %+v", m)
	}

	// presence violations
	mustNot(`{"type":"engine.offer","session_id":"s"}`)                  // no sdp
	mustNot(`{"type":"engine.candidate","session_id":"s"}`)              // no candidate key
	mustNot(`{"type":"engine.publish","session_id":"s","track":"mic"}`)  // no kind
	mustNot(`{"type":"engine.publish","session_id":"s","kind":"audio"}`) // no track
	mustNot(`{"type":"engine.subscribe","session_id":"s"}`)              // no track
	mustNot(`{"type":"engine.offer","sdp":"v=0"}`)                       // no session_id
	// kind is a closed set (bad_message at decode, not downstream)
	mustNot(`{"type":"engine.publish","session_id":"s","track":"t","kind":"smell"}`)
}

func TestHelloWSVersionMarkerDecodes(t *testing.T) {
	m, err := DecodeClientMessage([]byte(`{"type":"hello","token":"t","ws":2}`))
	if err != nil {
		t.Fatalf("hello with ws:2: %v", err)
	}
	if m.WSVersion != 2 {
		t.Fatalf("ws version: %d", m.WSVersion)
	}
	m, err = DecodeClientMessage([]byte(`{"type":"hello","token":"t"}`))
	if err != nil || m.WSVersion != 0 {
		t.Fatalf("legacy hello keeps 0: %+v %v", m, err)
	}
}
