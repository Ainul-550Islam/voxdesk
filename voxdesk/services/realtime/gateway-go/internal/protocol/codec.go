package protocol

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Relay payload caps, compile-time policy next to the codec that enforces
// the envelope. The frame limit (16 KiB, config.MaxMessageBytes) already
// bounds the whole JSON frame; these bound the meaningful bodies so a
// signaling session can never become a covert bulk-transfer channel that
// doesn't even pretend to be media negotiation.
const (
	// MaxSDPLength caps an offer/answer body. Measured SDP for a bundled
	// audio session is a few KB; 12 KiB leaves headroom for a couple of
	// extra codec sections and is comfortably below the frame limit.
	MaxSDPLength = 12 * 1024
	// MaxCandidateBytes caps one ICE candidate object — a few hundred bytes
	// of host/srflx/relay fields in practice.
	MaxCandidateBytes = 1024
)

// DecodeClientMessage parses a wire frame with the internal protocol's
// discipline: a required "type", a known variant, and per-variant required
// fields detected by PRESENCE (so `{"type":"hello","token":""}` is a
// bad_message at decode time, not an ambiguous auth failure downstream).
func DecodeClientMessage(data []byte) (*ClientMessage, error) {
	var msg ClientMessage
	if err := json.Unmarshal(data, &msg); err != nil {
		return nil, err
	}
	switch msg.Type {
	case TypeHello, TypeSubscribe, TypeUnsubscribe, TypePing,
		TypeSessionStart, TypeSessionJoin, TypeSessionEnd,
		TypeOffer, TypeAnswer, TypeCandidate,
		TypeEngineOffer, TypeEngineCandidate, TypeEnginePublish,
		TypeEngineSubscribe, TypeEngineUnsubscribe:
	default:
		return nil, fmt.Errorf("unknown message type %q", msg.Type)
	}
	switch msg.Type {
	case TypeHello:
		if !hasField(data, "token") || msg.Token == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "token", msg.Type)
		}
	case TypeSubscribe, TypeUnsubscribe:
		if !hasField(data, "room") || msg.Room == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "room", msg.Type)
		}
	case TypeSessionJoin, TypeSessionEnd, TypeOffer, TypeAnswer, TypeCandidate,
		TypeEngineOffer, TypeEngineCandidate, TypeEnginePublish,
		TypeEngineSubscribe, TypeEngineUnsubscribe:
		if !hasField(data, "session_id") || msg.SessionID == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "session_id", msg.Type)
		}
	}
	switch msg.Type {
	case TypeOffer, TypeAnswer:
		if !hasField(data, "sdp") || msg.SDP == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "sdp", msg.Type)
		}
	case TypeCandidate, TypeEngineCandidate:
		// Presence by hasField, not by non-nil value: `{"candidate":null}`
		// is the end-of-candidates marker and MUST decode successfully.
		if !hasField(data, "candidate") {
			return nil, fmt.Errorf("missing required field %q for %s", "candidate", msg.Type)
		}
	case TypeEngineOffer:
		if !hasField(data, "sdp") || msg.SDP == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "sdp", msg.Type)
		}
	case TypeEnginePublish:
		if !hasField(data, "track") || msg.Track == "" || !hasField(data, "kind") || msg.Kind == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "track/kind", msg.Type)
		}
		if msg.Kind != "audio" && msg.Kind != "video" && msg.Kind != "data" {
			return nil, fmt.Errorf("unknown media kind %q for %s", msg.Kind, msg.Type)
		}
	case TypeEngineSubscribe, TypeEngineUnsubscribe:
		if !hasField(data, "track") || msg.Track == "" {
			return nil, fmt.Errorf("missing required field %q for %s", "track", msg.Type)
		}
	}
	return &msg, nil
}

// hasField distinguishes a missing key from a zero value — the one place
// Go's lenient decoding would otherwise diverge from the strictness the
// protocol relies on for validation.
func hasField(data []byte, key string) bool {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return false
	}
	_, ok := fields[key]
	return ok
}

// IsValidSDP is the deliberate shallowness of the relay's SDP check: it
// verifies the blob STARTS like SDP ("v=" is the version line, always first)
// and nothing more. The edge routes negotiation; it does not parse media,
// and a parser here would be one more thing the browser and the media plane
// have to agree with US about. Overlong bodies are refused separately
// (MaxSDPLength, surfaced as signal_too_large).
func IsValidSDP(sdp string) bool {
	return strings.HasPrefix(strings.TrimSpace(sdp), "v=")
}

// CandidateIsObject reports whether raw is a JSON object (the only shape a
// trickled candidate may take, besides the null end marker which relays
// untouched) and within the relay cap. The end-of-candidates forms —
// literal null and {"candidate":""} — pass by design.
func CandidateIsObject(raw json.RawMessage) bool {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return true // end-of-candidates marker
	}
	if len(raw) > MaxCandidateBytes {
		return false
	}
	return strings.HasPrefix(trimmed, "{") && strings.HasSuffix(trimmed, "}")
}

// Marshal serializes a server frame for the wire.
func Marshal(msg any) ([]byte, error) {
	return json.Marshal(msg)
}
