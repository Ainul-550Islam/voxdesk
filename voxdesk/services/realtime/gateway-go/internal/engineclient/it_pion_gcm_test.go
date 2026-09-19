//go:build it

package engineclient_test

// Live GCM-profile end-to-end proof with TWO GENUINE WEBRTC USER AGENTS
// pinned to AEAD_AES_256_GCM ONLY. The engine now offers GCM first
// (RFC 7714 Phase A): a client whose offer contains ONLY GCM-256 cannot
// POSSIBLY land on CM — so media surviving the round trip here is the
// airborne acceptance of the engine's GCM crypto (keystream, IV, tag,
// GHASH and its RFC 3711-prf session derivation all byte-exact against
// pion's independent implementation).
//
//	VOXDESK_IT_ENGINE=1 VOXDESK_IT_ENGINE_BIN=... go test -tags it ./internal/engineclient -run PionGcm -v

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/pion/dtls/v3"
	rtpproto "github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

func TestLiveEnginePionGcmUaDtlsSrtpMediaLoopback(t *testing.T) {
	if os.Getenv("VOXDESK_IT_ENGINE") != "1" {
		t.Skip("set VOXDESK_IT_ENGINE=1 (and VOXDESK_IT_ENGINE_BIN)")
	}
	bin := os.Getenv("VOXDESK_IT_ENGINE_BIN")
	if bin == "" {
		t.Skip("VOXDESK_IT_ENGINE_BIN must point at a built media-engine-rs binary")
	}
	ctrl := "19370"
	spawnPionEngine(t, bin, ctrl, "19371")
	url := "http://127.0.0.1:" + ctrl
	client := engineclient.New(url, 500*time.Millisecond, nil, nil, func(string, ...any) {})

	dtlsBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)

	// GCM-256-ONLY UAs: the ONLY way these ever decrypt is a real
	// RFC 7714 GCM-256 session on both sides.
	seA := newPionSliceUA(t, dtls.SRTP_AEAD_AES_256_GCM)
	alice := newPionUAWithSE(t, "alice", true, seA)
	seB := newPionSliceUA(t, dtls.SRTP_AEAD_AES_256_GCM)
	bob := newPionUAWithSE(t, "bob", false, seB)
	alice.dialAndAnswer(t, client)
	bob.dialAndAnswer(t, client)
	awaitConnected(t, url, alice, bob)

	var aliceSSRC uint32
	for _, s := range alice.senders {
		if enc := s.GetParameters().Encodings; len(enc) > 0 {
			aliceSSRC = uint32(enc[0].SSRC)
			break
		}
	}
	if aliceSSRC == 0 {
		t.Fatalf("alice sender ssrc unresolved after negotiation")
	}
	ctxPub, cancelPub := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelPub()
	frames, err := client.Signal(ctxPub, "publish", map[string]any{
		"type": "publish", "session": alice.session, "track": "mic", "kind": "audio", "ssrc": aliceSSRC,
	})
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	for _, f := range frames {
		if f["type"] == "error" {
			t.Fatalf("publish with genuine client-ssrc refused: %v", frames)
		}
	}

	ctxSub, cancelSub := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelSub()
	if err := client.Subscribe(ctxSub, bob.session, "alice", "mic"); err != nil {
		t.Fatalf("subscribe: %v", err)
	}

	// In-band proof of the negotiated profile: pion exposes the
	// DTLS-SRTP chosen protection profile on its DTLS transport — pull
	// and pin it. If the engine EVER negotiated under GCM-only offer
	// anything but AEAD_AES_256_GCM, this fails closed.
	if got := readSrtpProfile(t, alice.pc); got != "AEAD_AES_256_GCM" {
		t.Fatalf("alice negotiated %s under a GCM-256-only offer — impossible unless the engine picked off-list", got)
	}

	marker := []byte{0xC0, 0xDE, 0xC0, 0xDE}
	pay := make([]byte, 64)
	copy(pay, marker)
	track, ok := alice.pc.GetSenders()[0].Track().(*webrtc.TrackLocalStaticSample)
	if !ok {
		t.Fatalf("alice outbound track is not a sample track")
	}
	go func() {
		for i := 0; i < 60; i++ {
			_ = track.WriteSample(media.Sample{Data: pay, Duration: 20 * time.Millisecond})
			time.Sleep(20 * time.Millisecond)
		}
	}()

	received := 0
	deadline := time.After(10 * time.Second)
	for received < 10 {
		select {
		case p := <-bob.got:
			if p.SSRC != aliceSSRC {
				t.Fatalf("bob received ssrc %x — routing must preserve lineage to alice ssrc %x", p.SSRC, aliceSSRC)
			}
			if len(p.Payload) < 4 || string(p.Payload[:4]) != string(marker) {
				t.Fatalf("bob received garbled payload %x — GCM decrypt on the engine or pion side is wrong", p.Payload[:4])
			}
			received++
		case <-deadline:
			t.Fatalf("bob received %d packets in 10s — media did not survive the GCM round-trip", received)
		}
	}

	dtlsAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)
	if dtlsAfter-dtlsBefore != 2 {
		t.Errorf("expected exactly 2 dtls handshakes established, delta=%d", dtlsAfter-dtlsBefore)
	}
	if refusedAfter != refusedBefore {
		t.Errorf("profile_refused advanced %d -> %d: engine produced off-profile outcomes?", refusedBefore, refusedAfter)
	}
	if v := metricsVal(t, url, "voxdesk_media_publish_refused_total"); v != 0 {
		t.Errorf("publish_refused=%d — explicit-ssrc publish must never conflict here", v)
	}

	_ = alice.pc.Close()
	_ = bob.pc.Close()
	fmt.Printf("pion GCM lane: %d AEAD_AES_256_GCM media frames verified end-to-end (alice ssrc=%x)\n",
		received, aliceSSRC)
}

// readSrtpProfile digs the negotiated DTLS-SRTP protection profile out
// of pion's transport stats. A SURFACE check, not a source of truth:
// the byte-level media assertions above are what actually convict.
func readSrtpProfile(t *testing.T, pc *webrtc.PeerConnection) string {
	t.Helper()
	report := pc.GetStats()
	for _, st := range report {
		if trs, ok := st.(webrtc.TransportStats); ok {
			if trs.ICERole == webrtc.ICERoleControlling || trs.ICERole == webrtc.ICERoleControlled {
				if trs.SRTPCipher != "" {
					return trs.SRTPCipher
				}
			}
		}
	}
	t.Fatal("no transport stats carrying srtpCipher — pion interop contract changed?")
	return ""
}

var _ = strings.Contains    // keep imports honest across build tags
var _ = http.StatusOK
var _ = rtpproto.Packet{}
