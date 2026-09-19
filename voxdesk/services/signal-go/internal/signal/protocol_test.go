package signal

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestMarshalWelcomeShape(t *testing.T) {
	data, err := MarshalServerMessage(newWelcome("abc-123"))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if string(m["type"]) != `"welcome"` {
		t.Fatalf("type = %s", m["type"])
	}
	if string(m["session_id"]) != `"abc-123"` {
		t.Fatalf("session_id = %s", m["session_id"])
	}
}

func TestMarshalSubscribedCarriesPeers(t *testing.T) {
	data, err := MarshalServerMessage(newSubscribed("room-a", 3))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if string(m["room"]) != `"room-a"` {
		t.Fatalf("room = %s", m["room"])
	}
	if string(m["peers"]) != "3" {
		t.Fatalf("peers = %s", m["peers"])
	}
}

func TestDeliveryEchoesPayloadSemantically(t *testing.T) {
	// The payload is preserved semantically; whitespace may be normalized on
	// re-marshal (the Rust side does the same via serde_json::Value).
	payload := json.RawMessage(`{"answer": 42, "nested": [1, 2]}`)
	data, err := MarshalServerMessage(newDelivery("room-a", "from-me", payload))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var d Delivery
	if err := json.Unmarshal(data, &d); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if d.Type != TypeDelivery || d.Room != "room-a" || d.From != "from-me" {
		t.Fatalf("unexpected delivery: %+v", d)
	}
	var want, got any
	if err := json.Unmarshal(payload, &want); err != nil {
		t.Fatalf("unmarshal want: %v", err)
	}
	if err := json.Unmarshal(d.Payload, &got); err != nil {
		t.Fatalf("unmarshal got: %v", err)
	}
	if !reflect.DeepEqual(want, got) {
		t.Fatalf("payload changed semantically: %v vs %v", got, want)
	}
}

func TestDecodeHello(t *testing.T) {
	msg, err := DecodeClientMessage([]byte(`{"type":"hello","tenant_id":"t-1"}`))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if msg.Type != TypeHello || msg.TenantID != "t-1" {
		t.Fatalf("unexpected: %+v", msg)
	}
}

func TestDecodePublish(t *testing.T) {
	msg, err := DecodeClientMessage([]byte(`{"type":"publish","room":"r","payload":{"a":1}}`))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if msg.Type != TypePublish || msg.Room != "r" {
		t.Fatalf("unexpected: %+v", msg)
	}
	if string(msg.Payload) != `{"a":1}` {
		t.Fatalf("payload = %s", msg.Payload)
	}
}

func TestDecodeRejectsUnknownType(t *testing.T) {
	if _, err := DecodeClientMessage([]byte(`{"type":"bogus"}`)); err == nil {
		t.Fatal("expected error for unknown type")
	}
}

func TestDecodeRejectsMissingType(t *testing.T) {
	if _, err := DecodeClientMessage([]byte(`{"room":"r"}`)); err == nil {
		t.Fatal("expected error for missing type")
	}
}

func TestDecodeRejectsMissingRequiredFields(t *testing.T) {
	cases := []string{
		`{"type":"hello"}`,                // missing tenant_id
		`{"type":"subscribe"}`,            // missing room
		`{"type":"unsubscribe"}`,          // missing room
		`{"type":"publish","room":"r"}`,   // missing payload
		`{"type":"publish","payload":{}}`, // missing room
	}
	for _, raw := range cases {
		if _, err := DecodeClientMessage([]byte(raw)); err == nil {
			t.Fatalf("expected error for %s", raw)
		}
	}
}

func TestDecodeRejectsMalformedJSON(t *testing.T) {
	if _, err := DecodeClientMessage([]byte(`{{{`)); err == nil {
		t.Fatal("expected error for malformed JSON")
	}
}

func TestDecodeAcceptsPingWithoutFields(t *testing.T) {
	msg, err := DecodeClientMessage([]byte(`{"type":"ping"}`))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if msg.Type != TypePing {
		t.Fatalf("type = %s", msg.Type)
	}
}

func TestErrorCodeConstantsMatchRust(t *testing.T) {
	// These strings are the wire vocabulary; keep them in lockstep with the
	// Rust hub's error codes.
	if CodeBadMessage != "bad_message" || CodeHelloRequired != "hello_required" || CodeHelloInvalid != "hello_invalid" {
		t.Fatalf("error code drift: %q %q %q", CodeBadMessage, CodeHelloRequired, CodeHelloInvalid)
	}
}
