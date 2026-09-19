// Package protocolgolden pins the gateway's wire format byte-for-byte.
//
// WHY GOLDENS LIVE AT THE MODULE ROOT: internal/protocol's own tests are
// behavioural (round-trips, validation). These are the CONTRACT tests — the
// exact bytes a dashboard client, the mobile SDK, or a future Rust
// re-implementation must parse. A change that breaks a golden is a wire-
// format change, and it should be impossible to make one accidentally
// (reviewers see the .golden diff; developers regenerate only deliberately,
// with `go test ./tests/protocol -update`).
package protocolgolden

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
)

var update = flag.Bool("update", false, "rewrite the .golden files with current output")

// fixed pins every clock to one instant so goldens are fully deterministic.
var fixed = time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)

func golden(t *testing.T, name string, frame any) {
	t.Helper()
	data, err := protocol.Marshal(frame)
	if err != nil {
		t.Fatalf("marshal %s: %v", name, err)
	}
	path := filepath.Join("testdata", name+".golden")
	if *update {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		// Trailing newline keeps .golden files diff/editor friendly; the
		// comparison below trims it, so the WIRE bytes are what is checked.
		if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
			t.Fatalf("write %s: %v", path, err)
		}
		t.Logf("updated %s", path)
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("missing golden %s (run with -update to create it): %v", path, err)
	}
	if string(bytes.TrimSpace(want)) != string(data) {
		t.Errorf("%s diverged from the wire contract:\n got: %s\nwant: %s", name, data, want)
	}
}

func TestServerFrameGoldens(t *testing.T) {
	golden(t, "welcome", protocol.NewWelcome("11111111-2222-3333-4444-555555555555", 10*time.Second, 20*time.Second, fixed))
	golden(t, "ready", protocol.NewReady("11111111-2222-3333-4444-555555555555", "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa", "admin", fixed.Add(time.Hour)))
	golden(t, "subscribed", protocol.NewSubscribed("calls", 3))
	golden(t, "unsubscribed", protocol.NewUnsubscribed("call:aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"))
	golden(t, "delivery_with_event_id", protocol.NewDelivery("calls", "call.updated", "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb",
		json.RawMessage(`{"call_id":"call-42","state":"active"}`), fixed))
	golden(t, "delivery_without_event_id", protocol.NewDelivery("metrics", "metrics.tick", "",
		json.RawMessage(`{"cpu":0.5}`), fixed))
	golden(t, "pong", protocol.NewPong(fixed))
	golden(t, "error", protocol.NewError(protocol.CodeRateLimited, "slow down"))
	golden(t, "session_started", protocol.NewSessionStarted("cccccccc-3333-3333-3333-cccccccccccc"))
	// Wire 1.2 land: joined/peer_joined carry the negotiated steer flag.
	// Both variants are pinned (false = the legacy/default path's bytes,
	// true = steered) so either mode's frame shape can only change on
	// deliberate review, never by accident.
	golden(t, "session_joined", protocol.NewSessionJoined("cccccccc-3333-3333-3333-cccccccccccc", false))
	golden(t, "session_peer_joined", protocol.NewSessionPeerJoined("cccccccc-3333-3333-3333-cccccccccccc", "responder", false))
	golden(t, "session_joined_steered", protocol.NewSessionJoined("cccccccc-3333-3333-3333-cccccccccccc", true))
	golden(t, "session_peer_joined_steered", protocol.NewSessionPeerJoined("cccccccc-3333-3333-3333-cccccccccccc", "responder", true))
	golden(t, "engine_answer", protocol.NewEngineAnswer("cccccccc-3333-3333-3333-cccccccccccc", "v=0\r\no=- 3 3 IN IP4 203.0.113.7\r\n"))
	golden(t, "engine_track_published", protocol.NewEngineTrackPublished("cccccccc-3333-3333-3333-cccccccccccc", "p-peer-1", "mic", "audio"))
	golden(t, "session_ended", protocol.NewSessionEnded("cccccccc-3333-3333-3333-cccccccccccc", "peer_disconnected"))
	golden(t, "signal_offer", protocol.NewSignalOffer("cccccccc-3333-3333-3333-cccccccccccc", "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"))
	golden(t, "signal_answer", protocol.NewSignalAnswer("cccccccc-3333-3333-3333-cccccccccccc", "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"))
	golden(t, "signal_candidate", protocol.NewSignalCandidate("cccccccc-3333-3333-3333-cccccccccccc",
		json.RawMessage(`{"candidate":"candidate:1 1 udp 2130706431 192.0.2.1 3478 typ host","sdpMid":"0","sdpMLineIndex":0}`)))
}
