package protocol

import "encoding/json"

// ClientMessage is the flat discriminator-dispatched frame a client sends.
// One struct with optional fields is the Go idiom for a tagged JSON union —
// exactly as the internal protocol does it.
//
// Field ownership by variant:
//
//	hello                 → token
//	subscribe/unsubscribe → room
//	session.join/end      → session_id
//	offer/answer          → session_id + sdp
//	candidate             → session_id + candidate
//	session.start, ping   → no payload fields
//
// Per-variant PRESENCE rules are enforced by DecodeClientMessage (codec.go),
// which is why fields appear here without validation tags: the wire grammar
// lives in exactly one place.
type ClientMessage struct {
	Type  string `json:"type"`
	Token string `json:"token,omitempty"`
	Room  string `json:"room,omitempty"`

	// WSVersion is the hello "ws" marker: the negotiated browser wire
	// level. 0/absent = v1.0/v1.1 (P2P relay only); 2+ = steer-capable
	// (v1.2: the five engine.* frames in message.go). It is read at hello
	// and pinned for the connection's lifetime.
	WSVersion int `json:"ws,omitempty"`

	// SessionID names the signaling session the frame acts on. It is never
	// the connection a message came FROM — the sender is always the socket
	// itself — so there is no field a client could use to address or
	// impersonate a peer.
	SessionID string `json:"session_id,omitempty"`

	// SDP carries the offer/answer body. Treated as OPAQUE here: the edge
	// validates its envelope (kind, size, rough shape) and never parses or
	// rewrites the media description — munging is the media plane's job.
	SDP string `json:"sdp,omitempty"`

	// Candidate carries one RTCIceCandidate object verbatim, or the
	// end-of-candidates marker (null, or an object with an empty
	// "candidate" string), which the peer needs to finish gathering.
	Candidate json.RawMessage `json:"candidate,omitempty"`

	// Track + Kind serve engine.publish / engine.subscribe / engine.
	// unsubscribe (v1.2): the caller-minted track identity and its media
	// kind ("audio"|"video"|"data"), per the engine's track vocabulary.
	Track string `json:"track,omitempty"`
	Kind  string `json:"kind,omitempty"`
}
