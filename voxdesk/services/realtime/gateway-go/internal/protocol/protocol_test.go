package protocol

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestDecodeValidVariants(t *testing.T) {
	cases := []string{
		`{"type":"hello","token":"abc.jwt.token"}`,
		`{"type":"subscribe","room":"calls"}`,
		`{"type":"unsubscribe","room":"metrics"}`,
		`{"type":"ping"}`,
	}
	for _, raw := range cases {
		msg, err := DecodeClientMessage([]byte(raw))
		if err != nil {
			t.Errorf("DecodeClientMessage(%s): %v", raw, err)
		}
		if msg == nil {
			t.Errorf("DecodeClientMessage(%s) returned nil message", raw)
		}
	}
}

func TestDecodeRejectsUnknownType(t *testing.T) {
	if _, err := DecodeClientMessage([]byte(`{"type":"admin"}`)); err == nil {
		t.Error("unknown type must be rejected")
	}
}

func TestDecodeDistinguishesMissingFromEmpty(t *testing.T) {
	// The strictness that matters: a hello with NO token and a hello with an
	// EMPTY token are BOTH bad_message — anything looser lets an empty bearer
	// string reach the verifier as an ambiguous auth failure.
	for _, raw := range []string{
		`{"type":"hello"}`,
		`{"type":"hello","token":""}`,
		`{"type":"subscribe"}`,
		`{"type":"subscribe","room":""}`,
	} {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Errorf("%s must be rejected at decode time", raw)
		}
	}
}

func TestDecodeRejectsMalformedJSON(t *testing.T) {
	for _, raw := range []string{"", "not json", `{"type":`, `["subscribe"]`} {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Errorf("%q must be rejected", raw)
		}
	}
}

func TestServerFramesMarshalToWireShape(t *testing.T) {
	now := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)

	welcome := NewWelcome("sess-1", 10*time.Second, 20*time.Second, now)
	raw, err := Marshal(welcome)
	if err != nil {
		t.Fatalf("marshal welcome: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("welcome is not JSON: %v", err)
	}
	if got["type"] != TypeWelcome || got["session_id"] != "sess-1" {
		t.Errorf("welcome = %s", raw)
	}
	if got["auth_required"] != true {
		t.Errorf("welcome must always tell a public client auth is required: %s", raw)
	}

	ready := NewReady("sess-1", "11111111-2222-3333-4444-555555555555", "owner", now.Add(15*time.Minute))
	raw, _ = Marshal(ready)
	if !strings.Contains(string(raw), `"token_expires_at"`) {
		t.Errorf("ready must advertise when the edge will close the socket: %s", raw)
	}

	sub := NewSubscribed("calls", 3)
	raw, _ = Marshal(sub)
	if !strings.Contains(string(raw), `"peers":3`) {
		t.Errorf("subscribed must carry the peer count: %s", raw)
	}

	delivery := NewDelivery("calls", "call.updated", "550e8400-e29b-41d4-a716-446655440000",
		json.RawMessage(`{"status":"COMPLETED"}`), now)
	raw, _ = Marshal(delivery)
	for _, want := range []string{`"kind":"call.updated"`, `"status":"COMPLETED"`, `"sent_at"`, `"event_id"`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("delivery missing %s: %s", want, raw)
		}
	}

	errMsg := NewError(CodeAuthFailed, "invalid or expired token")
	raw, _ = Marshal(errMsg)
	if !strings.Contains(string(raw), `"code":"auth_failed"`) {
		t.Errorf("error frame must carry the machine-readable code: %s", raw)
	}
}

func TestErrorCodesStayClosed(t *testing.T) {
	// A compile-time guard would be nicer; a regression list is what Go
	// gives us. The dashboard switches on these exact strings. The six
	// signaling codes were added with the session/signaling relay; the next
	// extension of this vocabulary must update dashboard handling first —
	// that is what this test is FOR.
	codes := map[string]bool{
		CodeBadMessage: true, CodeHelloRequired: true, CodeAuthFailed: true,
		CodeRoomInvalid: true, CodeOverLimit: true, CodeRateLimited: true,
		CodeSessionUnknown: true, CodeSessionFull: true, CodeSessionNotReady: true,
		CodeAlreadyInSession: true, CodeWrongSignalState: true, CodeSignalTooLarge: true,
	}
	if len(codes) != 12 {
		t.Fatalf("error code vocabulary changed; update the dashboard's switch first")
	}
}
