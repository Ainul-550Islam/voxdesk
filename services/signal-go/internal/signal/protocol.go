// Package signal implements the real-time signaling hub: a WebSocket fan-out
// service that is a differential counterpart of the Rust `voxdesk-signal`
// crate (services/control-plane/crates/signal). It speaks the exact same
// tagged-JSON wire protocol, so a client written against one server works
// against the other unchanged.
//
// Tenancy is structural: a room is keyed by (tenant_id, room), so a message
// can never cross a tenant boundary by accident.
package signal

import (
	"encoding/json"
	"fmt"
)

// Client message types — the "type" discriminator is snake_case to match the
// serde tagging on the Rust side.
const (
	TypeHello       = "hello"
	TypeSubscribe   = "subscribe"
	TypeUnsubscribe = "unsubscribe"
	TypePublish     = "publish"
	TypePing        = "ping"
)

// Server message types.
const (
	TypeWelcome      = "welcome"
	TypeSubscribed   = "subscribed"
	TypeUnsubscribed = "unsubscribed"
	TypeDelivery     = "delivery"
	TypeError        = "error"
	TypePong         = "pong"
)

// Error codes (mirrors the Rust hub's codes).
const (
	CodeBadMessage    = "bad_message"
	CodeHelloRequired = "hello_required"
	CodeHelloInvalid  = "hello_invalid"
)

// ClientMessage is the flat, discriminator-dispatched frame a client sends.
// One struct with optional fields is the Go idiom for a tagged JSON union.
type ClientMessage struct {
	Type     string          `json:"type"`
	TenantID string          `json:"tenant_id,omitempty"`
	Room     string          `json:"room,omitempty"`
	Payload  json.RawMessage `json:"payload,omitempty"`
}

// DecodeClientMessage parses a wire frame and validates it the way the Rust
// side's serde-tagged enum does: a required "type", a known variant, and the
// per-variant required fields. Anything else is a bad_message.
func DecodeClientMessage(data []byte) (*ClientMessage, error) {
	var msg ClientMessage
	if err := json.Unmarshal(data, &msg); err != nil {
		return nil, err
	}
	switch msg.Type {
	case TypeHello, TypeSubscribe, TypeUnsubscribe, TypePublish, TypePing:
		// known variant
	default:
		return nil, fmt.Errorf("unknown message type %q", msg.Type)
	}
	switch msg.Type {
	case TypeHello:
		if !hasField(data, "tenant_id") {
			return nil, fmt.Errorf("missing required field %q for %s", "tenant_id", msg.Type)
		}
	case TypeSubscribe, TypeUnsubscribe:
		if !hasField(data, "room") {
			return nil, fmt.Errorf("missing required field %q for %s", "room", msg.Type)
		}
	case TypePublish:
		if !hasField(data, "room") || !hasField(data, "payload") {
			return nil, fmt.Errorf("missing required field for %s", msg.Type)
		}
	}
	return &msg, nil
}

// hasField reports whether the JSON object contains key, distinguishing a
// missing field from a zero value — the one place Go's lenient decoding would
// otherwise diverge from serde's strictness.
func hasField(data []byte, key string) bool {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return false
	}
	_, ok := fields[key]
	return ok
}

// ServerMessage is implemented by every frame the server sends. Each variant
// carries its own "type" so json.Marshal emits the exact Rust-shaped object.
type ServerMessage interface {
	serverMessage()
}

// Welcome is the first frame: the server-assigned session id.
type Welcome struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
}

func (Welcome) serverMessage() {}

func newWelcome(sessionID string) Welcome {
	return Welcome{Type: TypeWelcome, SessionID: sessionID}
}

// Subscribed acknowledges a subscription with the room's current peer count.
type Subscribed struct {
	Type  string `json:"type"`
	Room  string `json:"room"`
	Peers int    `json:"peers"`
}

func (Subscribed) serverMessage() {}

func newSubscribed(roomName string, peers int) Subscribed {
	return Subscribed{Type: TypeSubscribed, Room: roomName, Peers: peers}
}

// Unsubscribed acknowledges leaving a room.
type Unsubscribed struct {
	Type string `json:"type"`
	Room string `json:"room"`
}

func (Unsubscribed) serverMessage() {}

func newUnsubscribed(roomName string) Unsubscribed {
	return Unsubscribed{Type: TypeUnsubscribed, Room: roomName}
}

// Delivery carries a published payload to one peer.
type Delivery struct {
	Type    string          `json:"type"`
	Room    string          `json:"room"`
	From    string          `json:"from"`
	Payload json.RawMessage `json:"payload"`
}

func (Delivery) serverMessage() {}

func newDelivery(roomName, from string, payload json.RawMessage) Delivery {
	return Delivery{Type: TypeDelivery, Room: roomName, From: from, Payload: payload}
}

// ErrorMessage is the server's refusal/validation frame.
type ErrorMessage struct {
	Type    string `json:"type"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (ErrorMessage) serverMessage() {}

func newError(code, message string) ErrorMessage {
	return ErrorMessage{Type: TypeError, Code: code, Message: message}
}

// Pong answers an application-level ping.
type Pong struct {
	Type string `json:"type"`
}

func (Pong) serverMessage() {}

func newPong() Pong { return Pong{Type: TypePong} }

// MarshalServerMessage serializes a server frame for the wire.
func MarshalServerMessage(msg ServerMessage) ([]byte, error) {
	return json.Marshal(msg)
}
