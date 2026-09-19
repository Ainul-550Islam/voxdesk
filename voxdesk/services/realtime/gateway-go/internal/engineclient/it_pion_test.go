//go:build it

package engineclient_test

// Live CM-profile end-to-end proof with TWO GENUINE WEBRTC USER AGENTS
// (pion/webrtc v4 — a real RFC stack: full-ICE agent, DTLS client,
// SRTP encrypt/decrypt). The engine runs as a spawned REAL binary, and
// both UAs are FORCED to offer exactly one SRTP protection profile —
// AES_CM_128_HMAC_SHA1_80 — so the RFC 5764 CM path must be used: if the
// engine negotiated anything else, or failed to decrypt/encrypt under
// the CM key material, the pion peers would see SRTP auth failures (a
// hard drop, never tolerated garbage) and the media assertions below
// would fail. This answers the question every reviewer asks — "does an
// honest, browser-shaped client ACTUALLY interop?" — without needing a
// browser in CI.
//
//	VOXDESK_IT_ENGINE=1 VOXDESK_IT_ENGINE_BIN=... go test -tags it ./internal/engineclient -run Pion -v

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/pion/dtls/v3"
	"github.com/pion/logging"
	rtpproto "github.com/pion/rtp"
	sdpv3 "github.com/pion/sdp/v3"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
	"github.com/voxdesk/realtime/gateway-go/internal/engineclient"
)

// pionUA is one genuine WebRTC peer: a PeerConnection with CM-only
// protection profiles, its engine signaling session, and what it heard.
type pionUA struct {
	name    string
	pc      *webrtc.PeerConnection
	session string
	senders []*webrtc.RTPSender
	got     chan *rtpproto.Packet
}

func newPionUA(t *testing.T, name string, send bool) *pionUA {
	// CM-only client policy — pin the RFC 3711 baseline lane.
	se := newPionSliceUA(t, dtls.SRTP_AES128_CM_HMAC_SHA1_80)
	return newPionUAWithSE(t, name, send, se)
}

// newPionSliceUA builds the SettingEngine side with an explicit profile
// slice: the order is what the UA OFFERS; what lands depends on the
// engine's preference over the intersection — exactly RFC 826/5764
// client-server negotiation shape.
func newPionSliceUA(t *testing.T, profiles ...dtls.SRTPProtectionProfile) webrtc.SettingEngine {
	t.Helper()
	se := webrtc.SettingEngine{}
	se.SetSRTPProtectionProfiles(profiles...)
	return se
}

func newPionUAWithSE(t *testing.T, name string, send bool, se webrtc.SettingEngine) *pionUA {
	t.Helper()
	// The engine speaks the minimal-SDP SFU dialect: its answer carries
	// no per-leg a=ssrc rows. pion's stock Unified Plan posture discards
	// undeclared-ssrc media under an ANSWER-shaped remote description,
	// so the UA explicitly accepts them (RFC 7943-compatible receiver
	// behavior). Still bound hard by the assertions below — SSRC lineage
	// and payload equality are checked by hand, so no laxity leaks in.
	se.SetHandleUndeclaredSSRCWithoutAnswer(true)
	if os.Getenv("VOXDESK_ICE_TRACE") == "1" {
		lf := logging.NewDefaultLoggerFactory()
		lf.DefaultLogLevel = logging.LogLevelTrace
		se.LoggerFactory = lf
	}
	me := &webrtc.MediaEngine{}
	if err := me.RegisterCodec(webrtc.RTPCodecParameters{
		RTPCodecCapability: webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2},
		PayloadType:        111,
	}, webrtc.RTPCodecTypeAudio); err != nil {
		t.Fatalf("register opus: %v", err)
	}
	// Browser baseline: every conformant browser registers the MID
	// header extension (RFC 8840); pion does NOT unless asked. Without
	// it, incoming media demultiplex falls back to declared-ssrc rows —
	// which a media server does not emit — and OnTrack never fires.
	if err := me.RegisterHeaderExtension(
		webrtc.RTPHeaderExtensionCapability{URI: sdpv3.SDESMidURI}, webrtc.RTPCodecTypeAudio); err != nil {
		t.Fatalf("register mid ext: %v", err)
	}
	api := webrtc.NewAPI(webrtc.WithSettingEngine(se), webrtc.WithMediaEngine(me))
	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatalf("peer connection: %v", err)
	}
	ua := &pionUA{name: name, pc: pc, got: make(chan *rtpproto.Packet, 64)}
	if send {
		track, err := webrtc.NewTrackLocalStaticSample(
			webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2},
			"audio", "mic-"+name)
		if err != nil {
			t.Fatalf("track: %v", err)
		}
		if _, err := pc.AddTrack(track); err != nil {
			t.Fatalf("add track: %v", err)
		}
	} else {
		if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeAudio,
			webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionRecvonly}); err != nil {
			t.Fatalf("transceiver: %v", err)
		}
	}
	pc.OnTrack(func(remote *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go func() {
			for {
				p, _, err := remote.ReadRTP()
				if err != nil {
					return
				}
				ua.got <- p
			}
		}()
	})
	return ua
}

// dialAndAnswer drives the production signaling shape against the real
// engine: join → gathered full offer → engine answer → SetRemote.
func (ua *pionUA) dialAndAnswer(t *testing.T, client *engineclient.Client) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	res, err := client.Join(ctx, "it-pion:room1", ua.name)
	if err != nil {
		t.Fatalf("%s join: %v", ua.name, err)
	}
	ua.session = res.Session

	offer, err := ua.pc.CreateOffer(nil)
	if err != nil {
		t.Fatalf("%s create offer: %v", ua.name, err)
	}
	if err := ua.pc.SetLocalDescription(offer); err != nil {
		t.Fatalf("%s set local: %v", ua.name, err)
	}
	<-webrtc.GatheringCompletePromise(ua.pc)
	fullSDP := ua.pc.LocalDescription().SDP
	if !strings.Contains(fullSDP, "a=candidate:") {
		t.Fatalf("%s gathered offer has no candidates — ICE unusable", ua.name)
	}

	frames, err := client.Signal(ctx, "offer", map[string]any{"type": "offer", "session": ua.session, "sdp": fullSDP})
	if err != nil {
		t.Fatalf("%s offer: %v", ua.name, err)
	}
	var answer string
	for _, f := range frames {
		if f["type"] == "answer" {
			answer, _ = f["sdp"].(string)
		}
	}
	if answer == "" {
		t.Fatalf("%s: no answer frame from engine: %v", ua.name, frames)
	}
	if err := ua.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: answer}); err != nil {
		t.Fatalf("%s set remote: %v", ua.name, err)
	}
	ua.senders = ua.pc.GetSenders()
}

func awaitConnected(t *testing.T, url2 string, uas ...*pionUA) {
	t.Helper()
	type res struct {
		name string
		ok   bool
	}
	done := make(chan res, len(uas))
	for _, ua := range uas {
		u := ua
		go func() {
			// Registering the handler AFTER SetLocal/SetRemote means the
			// Connected event may have fired already — poll from the
			// registered baseline, not just for new events.
			ch := make(chan webrtc.ICEConnectionState, 8)
			u.pc.OnICEConnectionStateChange(func(s webrtc.ICEConnectionState) { ch <- s })
			ticker := time.NewTicker(250 * time.Millisecond)
			defer ticker.Stop()
			timeout := time.After(20 * time.Second)
			for {
				cur := u.pc.ICEConnectionState()
				if cur == webrtc.ICEConnectionStateConnected || cur == webrtc.ICEConnectionStateCompleted {
					done <- res{u.name, true}
					return
				}
				if cur == webrtc.ICEConnectionStateFailed {
					done <- res{u.name, false}
					return
				}
				select {
				case <-ch:
				case <-ticker.C:
				case <-timeout:
					done <- res{u.name, false}
					return
				}
			}
		}()
	}
	seen := 0
	for seen < len(uas) {
		r := <-done
		seen++
		if !r.ok {
			st := metricsVal(t, url2, "voxdesk_media_udp_frames_total{kind=\"stun\"}")
			t.Fatalf("%s never reached ICE connected (ICE=%s conn=%s; engine stun_answered=%d)",
				r.name, pcState(r.name, uas), pcConn(r.name, uas), st)
		}
	}
}

// metricsVal scrapes one counter out of the engine /metrics exposition.
func metricsVal(t *testing.T, url, key string) int {
	t.Helper()
	resp, err := http.Get(url + "/metrics")
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	for _, line := range strings.Split(string(body), "\n") {
		if strings.HasPrefix(line, key) {
			v, _ := strconv.Atoi(strings.TrimSpace(line[len(key):]))
			return v
		}
	}
	return -1
}

func TestLiveEnginePionUaCmDtlsSrtpMediaLoopback(t *testing.T) {
	if os.Getenv("VOXDESK_IT_ENGINE") != "1" {
		t.Skip("set VOXDESK_IT_ENGINE=1 (and VOXDESK_IT_ENGINE_BIN)")
	}
	bin := os.Getenv("VOXDESK_IT_ENGINE_BIN")
	if bin == "" {
		t.Skip("VOXDESK_IT_ENGINE_BIN must point at a built media-engine-rs binary")
	}
	ctrl := "19360"
	spawnPionEngine(t, bin, ctrl, "19361")
	url := "http://127.0.0.1:" + ctrl
	client := engineclient.New(url, 500*time.Millisecond, nil, nil, func(string, ...any) {})

	dtlsBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedBefore := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)

	alice := newPionUA(t, "alice", true) // publisher: sends a track
	bob := newPionUA(t, "bob", false)    // subscriber: recvonly transceiver + OnTrack
	alice.dialAndAnswer(t, client)
	bob.dialAndAnswer(t, client)
	awaitConnected(t, url, alice, bob)

	// Publish alice's track with the SSRC pion actually chose — the v1.2
	// wire change: attribution binds the CLIENT's SSRC or media dies on
	// unknown-ssrc. No genuine UA passes this test without it.
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

	// Alice speaks; the engine must decrypt under HER outbound RFC5764
	// keys, route, and RE-ENCRYPT under bob's outbound split — reaching
	// bob decryptable under CM. Any wrong key shows up as SRTP auth
	// failure on bob's side (packet counts stay zero) or garbled payload.
	marker := []byte{0xDE, 0xAD, 0xBE, 0xEF}
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
				t.Fatalf("bob received garbled payload %x — CM decrypt on the engine or pion side is wrong", p.Payload[:4])
			}
			received++
		case <-deadline:
			t.Fatalf("bob received %d packets in 10s — media did not survive the CM round-trip", received)
		}
	}

	// The ledger: exactly 2 new CM handshakes and zero profile refusals
	// (with CM-only clients a GCM outcome would have been impossible —
	// anything but 2/0 means termination happened off the CM path).
	dtlsAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="established"}`)
	refusedAfter := metricsVal(t, url, `voxdesk_media_dtls_total{outcome="profile_refused"}`)
	if dtlsAfter-dtlsBefore != 2 {
		t.Errorf("expected exactly 2 dtls handshakes established, delta=%d (before=%d after=%d)",
			dtlsAfter-dtlsBefore, dtlsBefore, dtlsAfter)
	}
	if refusedAfter != refusedBefore {
		t.Errorf("profile_refused advanced %d -> %d: engine produced non-CM outcomes under a CM-only offer?",
			refusedBefore, refusedAfter)
	}
	if v := metricsVal(t, url, "voxdesk_media_publish_refused_total"); v != 0 {
		t.Errorf("publish_refused=%d — explicit-ssrc publish must never conflict here", v)
	}

	_ = alice.pc.Close()
	_ = bob.pc.Close()
	fmt.Printf("pion lane: %d CM-SRTP media frames verified end-to-end (alice ssrc=%x)\n", received, aliceSSRC)
}

// spawnPionEngine boots the real binary on the given ports and kills it
// when the test dies. Ports differ from the base it-lane so both tests
// can share a `go test -tags it` run sequentially without reuse races.
func spawnPionEngine(t *testing.T, bin, ctrlPort, udpPort string) {
	t.Helper()
	// The announced public IP must equal the address the kernel will
	// SOURCE engine packets from, or a genuine ICE agent rejects the
	// replies as coming from an unknown remote candidate ("no such
	// remote") — the same failure class as a mis-mapped NAT address in
	// production. Overridable via VOXDESK_ANNOUNCE_IP; by default we
	// announce the host's first non-loopback address (same-host traffic
	// hairpins through the local routing table on Linux and macOS).
	announce := os.Getenv("VOXDESK_ANNOUNCE_IP")
	if announce == "" {
		announce = firstNonLoopbackIP(t)
	}
	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(),
		"VOXDESK_PUBLIC_IP="+announce,
		"VOXDESK_CONTROL_ADDR=127.0.0.1:"+ctrlPort,
		"VOXDESK_PORT="+udpPort,
	)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start engine: %v", err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })

	probe := engineclient.New("http://127.0.0.1:"+ctrlPort, 500*time.Millisecond, nil, nil, func(string, ...any) {})
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := probe.HealthNow(context.Background()); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("engine never became healthy on control port %s", ctrlPort)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func pcState(name string, uas []*pionUA) string {
	for _, u := range uas {
		if u.name == name {
			return u.pc.ICEConnectionState().String()
		}
	}
	return "?"
}

func pcConn(name string, uas []*pionUA) string {
	for _, u := range uas {
		if u.name == name {
			return u.pc.ConnectionState().String()
		}
	}
	return "?"
}

// firstNonLoopbackIP resolves the address kernel-side replies will be
// sourced from on this host's primary interface, falling back to
// loopback when the box is air-gapped.
func firstNonLoopbackIP(t *testing.T) string {
	t.Helper()
	ifs, err := net.Interfaces()
	if err == nil {
		for _, ifi := range ifs {
			if ifi.Flags&net.FlagUp == 0 {
				continue
			}
			addrs, err := ifi.Addrs()
			if err != nil {
				continue
			}
			for _, a := range addrs {
				var ip net.IP
				switch v := a.(type) {
				case *net.IPNet:
					ip = v.IP
				case *net.IPAddr:
					ip = v.IP
				}
				if ip != nil && !ip.IsLoopback() && ip.To4() != nil {
					return ip.String()
				}
			}
		}
	}
	return "127.0.0.1"
}
