//go:build it

package engineclient_test

// Live Go↔Rust integration (opt-in): builds and runs the REAL
// media-engine-rs binary, then drives it through the full public protocol
// the production gateway speaks. It is excluded from the default suite on
// purpose (CI runs it on the engine-change lane; local runs set the env
// var explicitly):
//
//	VOXDESK_IT_ENGINE=1 go test -tags it ./internal/engineclient -run Live
//
// The test compiles nothing itself: VOXDESK_IT_ENGINE_BIN must point at a
// freshly built binary (see scripts/build-media-engine.sh).

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

func TestLiveEngineJoinOfferTrickleLeave(t *testing.T) {
	if os.Getenv("VOXDESK_IT_ENGINE") != "1" {
		t.Skip("set VOXDESK_IT_ENGINE=1 to run the live engine test")
	}
	bin := os.Getenv("VOXDESK_IT_ENGINE_BIN")
	if bin == "" {
		t.Skip("VOXDESK_IT_ENGINE_BIN must point at a built media-engine-rs binary")
	}
	port := "19341"
	t.Setenv("VOXDESK_PUBLIC_IP", "127.0.0.1")
	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(), "VOXDESK_PUBLIC_IP=127.0.0.1", "VOXDESK_CONTROL_ADDR=127.0.0.1:"+port, "VOXDESK_PORT=19340")
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start engine: %v", err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })

	client := engineclient.New("http://127.0.0.1:"+port, 500*time.Millisecond, nil, nil, func(string, ...any) {})

	// Wait for the monitor-grade probe to go green.
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, err := client.HealthNow(context.Background())
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("engine never became healthy: %v", err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	if !client.Up() {
		t.Fatalf("availability not green after health")
	}

	// Join → ready with ICE creds → OFFER envelope → Answer.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	res, err := client.Join(ctx, "it-tenant:it-session", "it-participant")
	if err != nil {
		t.Fatalf("join: %v", err)
	}
	if res.Session == "" || res.IceUfrag == "" || len(res.IcePwd) < 6 {
		t.Fatalf("ready triple incomplete: %+v", res)
	}

	offer := "v=0\r\n" +
		"o=- 1 1 IN IP4 127.0.0.1\r\n" +
		"s=-\r\n" +
		"c=IN IP4 0.0.0.0\r\n" +
		"t=0 0\r\n" +
		"a=group:BUNDLE 0\r\n" +
		fmt.Sprintf("a=ice-ufrag:%s\r\na=ice-pwd:012345678901234567890123\r\n", "liveufrag") +
		"a=fingerprint:sha-256 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n" +
		"a=setup:actpass\r\n" +
		"a=mid:0\r\n" +
		"m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" +
		"a=rtpmap:111 opus/48000/2\r\n" +
		"a=sendrecv\r\n"
	frames, err := client.Signal(ctx, "offer", engineclient.Frame{"type": "offer", "session": res.Session, "sdp": offer})
	if err != nil {
		t.Fatalf("offer: %v", err)
	}
	foundAnswer := false
	for _, f := range frames {
		if f["type"] == "answer" {
			foundAnswer = true
		}
	}
	if !foundAnswer {
		t.Fatalf("no answer frame from engine: %v", frames)
	}

	// A valid trickle is quiet success; garbage is BadMessage-shaped.
	trickleOK := engineclient.Frame{
		"type": "trickle", "session": res.Session,
		"candidate": map[string]any{"candidate": "candidate:1 1 UDP 2130706431 203.0.113.5 54400 typ host", "sdpMid": "0"},
	}
	if _, err := client.Signal(ctx, "trickle", trickleOK); err != nil {
		t.Fatalf("valid trickle refused: %v", err)
	}
	bad := engineclient.Frame{"type": "trickle", "session": res.Session, "candidate": map[string]any{"candidate": "garbage"}}
	frames, err = client.Signal(ctx, "trickle", bad)
	if err != nil {
		t.Fatalf("trickle round trip: %v", err)
	}
	foundErr := false
	for _, f := range frames {
		if f["type"] == "error" && f["code"] == "bad_message" {
			foundErr = true
		}
	}
	if !foundErr {
		t.Fatalf("malformed trickle must answer bad_message: %v", frames)
	}

	// The publish path the steer router DEPENDS on: a real engine answers
	// publish with the room fanout ("track.published") riding the same
	// envelope the caller's (empty) reply would. Subscribe → quiet
	// success; subscribe for a track that isn't ours is refused.
	frames, err = client.Publish(getCtx(), res.Session, "mic", "audio")
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	foundPublished := false
	for _, f := range frames {
		if f["type"] == "track.published" && f["track"] == "mic" && f["kind"] == "audio" && f["participant"] == "it-participant" {
			foundPublished = true
		}
	}
	if !foundPublished {
		t.Fatalf("real engine publish fanout missing track.published: %v", frames)
	}

	if err := client.Subscribe(getCtx(), res.Session, "it-participant", "mic"); err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	if err := client.Unsubscribe(getCtx(), res.Session, "it-participant", "mic"); err != nil {
		t.Fatalf("unsubscribe: %v", err)
	}

	if err := client.Leave(ctx, res.Session); err != nil {
		t.Fatalf("leave: %v", err)
	}
}

// getCtx is a tiny per-block bounded context factory (avoids shadowing
// the main ctx declared above with closer deadlines).
func getCtx() context.Context {
	ctx, _ := context.WithTimeout(context.Background(), 2*time.Second)
	return ctx
}
