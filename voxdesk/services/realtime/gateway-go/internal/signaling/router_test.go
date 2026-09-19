package signaling

import (
	"strings"
	"testing"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/hub"
	"github.com/voxdesk/realtime/gateway-go/internal/observability/metrics"
	"github.com/voxdesk/realtime/gateway-go/internal/protocol"
	"github.com/voxdesk/realtime/gateway-go/internal/session"
)

// fakeConn is the hub.Subscriber stand-in: a recorder that behaves like a
// websocket with an infinitely roomy queue (the drop path is tested
// separately).
type fakeConn struct {
	id     string
	tenant string
	frames []any
}

func (f *fakeConn) Session() string              { return f.id }
func (f *fakeConn) Tenant() string               { return f.tenant }
func (f *fakeConn) RequestClose(_ int, _ string) {}
func (f *fakeConn) Enqueue(msg any) bool         { f.frames = append(f.frames, msg); return true }

// rig wires a router over recorded connections.
type rig struct {
	router *Router
	conns  map[string]*fakeConn
	reg    *metrics.Registry
}

func newRig(ttl time.Duration) *rig {
	r := &rig{conns: map[string]*fakeConn{}}
	r.reg = metrics.New()
	m := session.NewManager(64, ttl)
	lookup := func(connID string) (hub.Subscriber, bool) {
		c, ok := r.conns[connID]
		if !ok {
			return nil, false
		}
		return c, true
	}
	r.router = NewRouter(m, lookup, r.reg)
	return r
}

func (r *rig) conn(id, tenant string) *fakeConn {
	c := &fakeConn{id: id, tenant: tenant}
	r.conns[id] = c
	return c
}

func (r *rig) msg(from *fakeConn, wire string) *protocol.ClientMessage {
	msg, err := protocol.DecodeClientMessage([]byte(wire))
	if err != nil {
		panic("test wire frame must decode: " + wire)
	}
	r.router.HandleMessage(from, msg)
	return msg
}

const sdp = "v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

func TestFullNegotiationRoundTrip(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")

	// --- session.start: the initiator is acked with its role and the id.
	a.frames = nil
	r.msg(a, `{"type":"session.start"}`)
	if len(a.frames) != 1 {
		t.Fatalf("start ack frames = %v", a.frames)
	}
	started, ok := a.frames[0].(protocol.SessionStarted)
	if !ok {
		t.Fatalf("start ack type = %T", a.frames[0])
	}
	if started.Role != protocol.RoleInitiator || started.SessionID == "" {
		t.Fatalf("start ack = %+v", started)
	}
	id := started.SessionID

	// --- session.join: responder acked, initiator woken.
	a.frames, b.frames = nil, nil
	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)
	if len(b.frames) != 1 || len(a.frames) != 1 {
		t.Fatalf("join frames a=%v b=%v", a.frames, b.frames)
	}
	if joined := b.frames[0].(protocol.SessionJoined); joined.Role != protocol.RoleResponder {
		t.Errorf("join ack = %+v", joined)
	}
	if pj := a.frames[0].(protocol.SessionPeerJoined); pj.PeerRole != protocol.RoleResponder {
		t.Errorf("peer-joined notice = %+v", pj)
	}

	// --- offer → forwarded to the peer VERBATIM.
	a.frames, b.frames = nil, nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if len(b.frames) != 1 || len(a.frames) != 0 {
		t.Fatalf("offer frames a=%v b=%v", a.frames, b.frames)
	}
	if offer := b.frames[0].(protocol.SignalOffer); offer.SDP != sdp {
		t.Errorf("sdp was rewritten in transit:\n%s", offer.SDP)
	}

	// --- answer → back to the offerer.
	a.frames, b.frames = nil, nil
	r.msg(b, `{"type":"answer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if ans := a.frames[0].(protocol.SignalAnswer); ans.SDP != sdp {
		t.Errorf("answer sdp = %q", ans.SDP)
	}

	// --- candidates relay both ways, byte-for-byte, null included.
	a.frames, b.frames = nil, nil
	r.msg(a, `{"type":"candidate","session_id":"`+id+`","candidate":{"candidate":"candidate:1 1 udp 2130706431 10.0.0.1 9 typ host","sdpMid":"0"}}`)
	if cand := b.frames[0].(protocol.SignalCandidate); !strings.Contains(string(cand.Candidate), "10.0.0.1") {
		t.Errorf("candidate payload = %s", cand.Candidate)
	}
	r.msg(b, `{"type":"candidate","session_id":"`+id+`","candidate":null}`)
	if cand := a.frames[0].(protocol.SignalCandidate); string(cand.Candidate) != "null" {
		t.Errorf("end-of-candidates must relay as null, got %s", cand.Candidate)
	}

	// --- session.end: BOTH members hear it, sender included.
	a.frames, b.frames = nil, nil
	r.msg(a, `{"type":"session.end","session_id":"`+id+`"}`)
	if len(a.frames) != 1 || len(b.frames) != 1 {
		t.Fatalf("end frames a=%v b=%v", a.frames, b.frames)
	}
	if end := b.frames[0].(protocol.SessionEnded); end.Reason != session.ReasonMemberEnded {
		t.Errorf("end reason = %q", end.Reason)
	}

	if r.reg == nil {
		t.Fatal("registry unreachable")
	}
	// Three relayed payloads (offer, answer, first candidate... plus the
	// null candidate) and the session gauges balance.
	rendered := r.reg.Render(0, 0)
	for _, want := range []string{
		"voxdesk_gateway_signal_sessions_current 0",
		"voxdesk_gateway_signal_sessions_total 1",
		"voxdesk_gateway_signal_relayed_total 4",
	} {
		if !strings.Contains(rendered, want) {
			t.Errorf("metrics missing %q", want)
		}
	}
}

// jsonString renders s as a JSON string literal for embedding in wire frames.
func jsonString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\r':
			b.WriteString(`\r`)
		case '\n':
			b.WriteString(`\n`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

func TestPayloadGuardsRefuseBeforeTheStateMachine(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID

	// Non-SDP string as sdp.
	a.frames = nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":"not-sdp-at-all"}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeBadMessage {
		t.Errorf("non-sdp offer code = %q", errFrame.Code)
	}

	// Oversize SDP → the DISTINCT signal_too_large, so a legitimate client
	// can tell "fix your message" from "you hit a documented cap".
	a.frames = nil
	big := "v=0\r\n" + strings.Repeat("a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n", protocol.MaxSDPLength/50)
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(big)+`}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeSignalTooLarge {
		t.Errorf("oversize offer code = %q", errFrame.Code)
	}

	// Candidate that is not an object.
	a.frames = nil
	r.msg(a, `{"type":"candidate","session_id":"`+id+`","candidate":[1,2,3]}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeBadMessage {
		t.Errorf("array candidate code = %q", errFrame.Code)
	}
}

func TestJoinUnknownAndCrossTenantAreTheSameRefusal(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	x := r.conn("conn-x", "tenant-2")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID

	x.frames = nil
	r.msg(x, `{"type":"session.join","session_id":"`+id+`"}`)
	if errFrame := lastError(t, x); errFrame.Code != protocol.CodeSessionUnknown {
		t.Fatalf("cross-tenant join code = %q", errFrame.Code)
	}
	// An UNSEATED connection of the same tenant joining a made-up id gets
	// the same collapse (conn-a already holds a seat, so ITS refusal would
	// honestly be already_in_session — its own constraint, not session data).
	fresh := r.conn("conn-f", "tenant-1")
	fresh.frames = nil
	r.msg(fresh, `{"type":"session.join","session_id":"11111111-2222-3333-4444-555555555555"}`)
	if errFrame := lastError(t, fresh); errFrame.Code != protocol.CodeSessionUnknown {
		t.Fatalf("unknown join code = %q", errFrame.Code)
	}
	// Both refusal messages are the SAME TEXT — the collapsing is total.
	if lastError(t, x).Message != lastError(t, fresh).Message {
		t.Errorf("refusal texts must not vary: %q vs %q", lastError(t, x).Message, lastError(t, fresh).Message)
	}
}

func TestPhaseRefusalsCarryTheSpecificCodes(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID

	// Offer before the peer joins → session_not_ready.
	a.frames = nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeSessionNotReady {
		t.Errorf("lone offer code = %q", errFrame.Code)
	}

	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)

	// Glare guard: after one offer, the second is wrong_signal_state.
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	a.frames = nil
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeWrongSignalState {
		t.Errorf("glare code = %q", errFrame.Code)
	}

	// Already-seated connections cannot seize a second session.
	a.frames = nil
	r.msg(a, `{"type":"session.start"}`)
	if errFrame := lastError(t, a); errFrame.Code != protocol.CodeAlreadyInSession {
		t.Errorf("double start code = %q", errFrame.Code)
	}
}

func TestConnClosedEndsTheSessionForTheSurvivor(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID
	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)

	b.frames = nil
	r.router.ConnClosed(a)
	if len(b.frames) != 1 {
		t.Fatalf("survivor frames = %v", b.frames)
	}
	if end := b.frames[0].(protocol.SessionEnded); end.Reason != session.ReasonPeerDisconnected {
		t.Errorf("end reason = %q", end.Reason)
	}
}

func TestReapJoinTimeoutIsDeliveredToTheWaiter(t *testing.T) {
	r := newRig(time.Nanosecond) // expired the moment it exists
	a := r.conn("conn-a", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	a.frames = nil

	// Tick the reaper directly (StartReaper's ticker cadence is what main
	// does; the sweep itself is what this test owns).
	r.router.emit(r.router.Manager().Reap())
	if len(a.frames) != 1 {
		t.Fatalf("reap frames = %v", a.frames)
	}
	if end := a.frames[0].(protocol.SessionEnded); end.Reason != session.ReasonJoinTimeout {
		t.Errorf("reap reason = %q", end.Reason)
	}
}

func TestNoFrameEverLeavesTheSessionPair(t *testing.T) {
	r := newRig(time.Minute)
	a := r.conn("conn-a", "tenant-1")
	b := r.conn("conn-b", "tenant-1")
	innocent := r.conn("conn-i", "tenant-1")

	r.msg(a, `{"type":"session.start"}`)
	id := a.frames[0].(protocol.SessionStarted).SessionID
	r.msg(b, `{"type":"session.join","session_id":"`+id+`"}`)
	r.msg(a, `{"type":"offer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	r.msg(b, `{"type":"answer","session_id":"`+id+`","sdp":`+jsonString(sdp)+`}`)
	r.msg(a, `{"type":"candidate","session_id":"`+id+`","candidate":null}`)
	r.msg(b, `{"type":"session.end","session_id":"`+id+`"}`)

	if len(innocent.frames) != 0 {
		t.Fatalf("an uninvolved connection observed %d frames", len(innocent.frames))
	}
}

// lastError asserts the tail frame of conn is an error and returns it.
func lastError(t *testing.T, conn *fakeConn) protocol.ErrorMessage {
	t.Helper()
	if len(conn.frames) == 0 {
		t.Fatalf("expected an error frame on %s, saw none", conn.id)
	}
	errFrame, ok := conn.frames[len(conn.frames)-1].(protocol.ErrorMessage)
	if !ok {
		t.Fatalf("tail frame on %s = %T, want protocol.ErrorMessage", conn.id, conn.frames[len(conn.frames)-1])
	}
	return errFrame
}
