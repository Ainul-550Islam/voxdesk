// Package protocol defines the gateway's wire frames: the tagged-JSON
// messages exchanged with browsers at the public WebSocket edge.
//
// The vocabulary deliberately mirrors the internal signaling protocol
// (services/signal-go/internal/signal, itself the differential counterpart
// of the Rust voxdesk-signal crate): hello/subscribe/unsubscribe/ping in,
// welcome/subscribed/unsubscribed/delivery/error/pong out. A client library
// that speaks to the internal hub needs only the auth frame added to speak
// to the public edge.
//
// The security difference from the internal hub is what "hello" carries:
// here it carries a verified access token, and the tenant a connection may
// see is pinned to the token's `tid` claim at that moment. No message after
// hello can name a tenant — the frame schema does not have a tenant field.
//
// This file is the message TYPE vocabulary (client and server frame names,
// signaling roles) plus the server frame structs. Envelopes live in
// envelope.go, the error vocabulary in error.go, and wire encoding plus
// payload guards in codec.go — one concern per file, matching the layout
// the maintainers use elsewhere in this service.
package protocol

import (
	"encoding/json"
	"time"
)

// Client message types. The first four are the realtime-notice protocol this
// edge shipped with; the six signaling types extend it for WebRTC session
// setup (see internal/signaling) and live behind the same pre-auth gate:
// none of them does anything before hello succeeds.
const (
	TypeHello       = "hello"
	TypeSubscribe   = "subscribe"
	TypeUnsubscribe = "unsubscribe"
	TypePing        = "ping"

	TypeSessionStart = "session.start" // create a 2-member signaling session as its initiator
	TypeSessionJoin  = "session.join"  // join an existing session as the responder
	TypeSessionEnd   = "session.end"   // end a session the sender belongs to
	TypeOffer        = "offer"         // SDP offer to the peer (any active member, one at a time)
	TypeAnswer       = "answer"        // SDP answer to an outstanding offer (the non-offerer)
	TypeCandidate    = "candidate"     // one trickled ICE candidate to the peer

	// Wire vocabulary 1.2 — the SFU steer. These frames are accepted ONLY
	// on sessions whose negotiated mode is steered (both members sent
	// "ws":2 in hello and the gateway's engine link is in v1.2|force
	// mode); on plain sessions they refuse with steer_mode_not_active.
	// Field shapes mirror their engine-side counterparts, keyed on the
	// GATEWAY session id — the router translates to engine session ids
	// internally (browsers never learn engine capabilities).
	TypeEngineOffer       = "engine.offer"       // SDP offer to the media engine for this session
	TypeEngineCandidate   = "engine.candidate"   // one trickled candidate for the engine session
	TypeEnginePublish     = "engine.publish"     // declare a local track: {session_id, track, kind}
	TypeEngineSubscribe   = "engine.subscribe"   // ask for the peer's track: {session_id, track}
	TypeEngineUnsubscribe = "engine.unsubscribe" // withdraw: {session_id, track}
)

// Server message types.
const (
	TypeWelcome      = "welcome"
	TypeReady        = "ready"
	TypeSubscribed   = "subscribed"
	TypeUnsubscribed = "unsubscribed"
	TypeDelivery     = "delivery"
	TypeError        = "error"
	TypePong         = "pong"

	TypeSessionStarted    = "session.started"     // session created; sender is its initiator
	TypeSessionJoined     = "session.joined"      // sender joined as the responder
	TypeSessionPeerJoined = "session.peer_joined" // the other member arrived
	TypeSessionEnded      = "session.ended"       // session is over, with a reason code
	TypeSignalOffer       = "signal.offer"        // forwarded SDP offer
	TypeSignalAnswer      = "signal.answer"       // forwarded SDP answer
	TypeSignalCandidate   = "signal.candidate"    // forwarded ICE candidate

	// Wire vocabulary 1.2 server frames for the steered path.
	TypeEngineAnswer         = "engine.answer"          // the engine's SDP answer to engine.offer
	TypeEngineTrackPublished = "engine.track_published" // the peer minted a track on the engine
)

// Signaling roles. Exactly two members ever exist in a session: its creator
// and the one peer who joins it. An open-ended membership model would
// re-invent rooms; a media session is point-to-point.
const (
	RoleInitiator = "initiator"
	RoleResponder = "responder"
)

// ---------------------------------------------------------------------------
// Realtime-notice server frames (unchanged from the original protocol).
// ---------------------------------------------------------------------------

// Welcome is the first frame on every connection, sent before any client
// traffic is read. It tells the browser how to proceed: authenticate, and
// how long it has.
type Welcome struct {
	Type            string `json:"type"`
	SessionID       string `json:"session_id"`
	AuthRequired    bool   `json:"auth_required"`
	AuthTimeoutSecs int    `json:"auth_timeout_secs"`
	HeartbeatSecs   int    `json:"heartbeat_secs"`
	ServerTimeUTC   string `json:"server_time"`
}

// NewWelcome builds the greeting for one fresh session.
func NewWelcome(sessionID string, authTimeout, heartbeat time.Duration, now time.Time) Welcome {
	return Welcome{
		Type:            TypeWelcome,
		SessionID:       sessionID,
		AuthRequired:    true,
		AuthTimeoutSecs: int(authTimeout / time.Second),
		HeartbeatSecs:   int(heartbeat / time.Second),
		ServerTimeUTC:   now.UTC().Format(time.RFC3339),
	}
}

// Ready acknowledges successful authentication and pins the session to the
// token's tenant. After this frame the client may subscribe and signal.
type Ready struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	TenantID  string `json:"tenant_id"`
	Role      string `json:"role"`
	// TokenExpiresAt tells the client when the edge will close this socket
	// (the edge never outlives the access token that opened it), so it can
	// reconnect with the refreshed token before the drop.
	TokenExpiresAt string `json:"token_expires_at"`
}

// NewReady builds the post-auth acknowledgement.
func NewReady(sessionID, tenantID, role string, expiresAt time.Time) Ready {
	return Ready{
		Type:           TypeReady,
		SessionID:      sessionID,
		TenantID:       tenantID,
		Role:           role,
		TokenExpiresAt: expiresAt.UTC().Format(time.RFC3339),
	}
}

// Subscribed acknowledges a subscription with the room's peer count so a
// wallboard can show "2 dashboards watching".
type Subscribed struct {
	Type  string `json:"type"`
	Room  string `json:"room"`
	Peers int    `json:"peers"`
}

// NewSubscribed builds the ack.
func NewSubscribed(room string, peers int) Subscribed {
	return Subscribed{Type: TypeSubscribed, Room: room, Peers: peers}
}

// Unsubscribed acknowledges leaving a room. Leaving a room the session never
// joined is acknowledged identically — idempotent, never an error.
type Unsubscribed struct {
	Type string `json:"type"`
	Room string `json:"room"`
}

// NewUnsubscribed builds the ack.
func NewUnsubscribed(room string) Unsubscribed {
	return Unsubscribed{Type: TypeUnsubscribed, Room: room}
}

// Delivery carries one published event to one subscriber. EventID lets a
// client dedupe across a reconnect; SentAt lets it render staleness.
type Delivery struct {
	Type    string          `json:"type"`
	Room    string          `json:"room"`
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
	EventID string          `json:"event_id,omitempty"`
	SentAt  string          `json:"sent_at"`
}

// NewDelivery builds one fan-out frame.
func NewDelivery(room, kind, eventID string, payload json.RawMessage, now time.Time) Delivery {
	return Delivery{
		Type:    TypeDelivery,
		Room:    room,
		Kind:    kind,
		Payload: payload,
		EventID: eventID,
		SentAt:  now.UTC().Format(time.RFC3339),
	}
}

// Pong answers an application-level ping (the WS-level ping/pong is handled
// by the transport heartbeat; this exists for latency probes through the
// full JSON path, same as the internal protocol).
type Pong struct {
	Type          string `json:"type"`
	ServerTimeUTC string `json:"server_time"`
}

// NewPong builds the reply.
func NewPong(now time.Time) Pong {
	return Pong{Type: TypePong, ServerTimeUTC: now.UTC().Format(time.RFC3339)}
}

// ---------------------------------------------------------------------------
// Signaling server frames. None of these leak the peer's identity beyond the
// shared session id — the two members already know each other by out-of-band
// arrangement (the session id itself is the capability: 122 random bits,
// joinable only from inside the same tenant).
// ---------------------------------------------------------------------------

// SessionStarted confirms session.start. The session id is the join
// capability the initiator shares with its intended peer (out of band).
type SessionStarted struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Role      string `json:"role"`
}

// NewSessionStarted builds the start confirmation.
func NewSessionStarted(sessionID string) SessionStarted {
	return SessionStarted{Type: TypeSessionStarted, SessionID: sessionID, Role: RoleInitiator}
}

// SessionJoined confirms session.join. The joiner is always the responder.
type SessionJoined struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Role      string `json:"role"`
	// Steer is the negotiated media path for this session: true means the
	// SFU (engine.* frames) owns media; false means P2P relay. Always
	// emitted on wire 1.2+ so a v1.2 client never has to guess legacy
	// defaults — additive and therefore safe for v1.1 parsers.
	Steer bool `json:"steer"`
}

// NewSessionJoined builds the join confirmation for the negotiated mode.
func NewSessionJoined(sessionID string, steer bool) SessionJoined {
	return SessionJoined{Type: TypeSessionJoined, SessionID: sessionID, Role: RoleResponder, Steer: steer}
}

// SessionPeerJoined tells the waiting initiator that the responder arrived —
// the cue to createOffer, for clients that follow the initiator-offers
// convention.
type SessionPeerJoined struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	PeerRole  string `json:"peer_role"`
	Steer     bool   `json:"steer"` // see SessionJoined.Steer
}

// NewSessionPeerJoined builds the peer-arrived notice for the negotiated
// mode.
func NewSessionPeerJoined(sessionID, peerRole string, steer bool) SessionPeerJoined {
	return SessionPeerJoined{Type: TypeSessionPeerJoined, SessionID: sessionID, PeerRole: peerRole, Steer: steer}
}

// SessionEnded ends a session for every member that still cares. Reason is
// a machine-readable closed vocabulary owned by the session package
// ("member_ended", "peer_disconnected", "join_timeout").
type SessionEnded struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	Reason    string `json:"reason"`
}

// NewSessionEnded builds the end notice.
func NewSessionEnded(sessionID, reason string) SessionEnded {
	return SessionEnded{Type: TypeSessionEnded, SessionID: sessionID, Reason: reason}
}

// SignalOffer is a forwarded SDP offer. The body passes through untouched:
// SDP munging belongs to the media plane, and this edge's threat model is
// routing, never rewriting.
type SignalOffer struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

// NewSignalOffer builds the forwarded offer.
func NewSignalOffer(sessionID, sdp string) SignalOffer {
	return SignalOffer{Type: TypeSignalOffer, SessionID: sessionID, SDP: sdp}
}

// SignalAnswer is a forwarded SDP answer.
type SignalAnswer struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

// NewSignalAnswer builds the forwarded answer.
func NewSignalAnswer(sessionID, sdp string) SignalAnswer {
	return SignalAnswer{Type: TypeSignalAnswer, SessionID: sessionID, SDP: sdp}
}

// SignalCandidate is a forwarded ICE candidate, carried as raw JSON because
// the edge must not impose a schema on a structure the browser and the
// media plane already agree on (RTCIceCandidate: candidate/sdpMid/
// sdpMLineIndex/usernameFragment). An end-of-candidates marker (a null or
// empty-string candidate) relays identically — the peer needs it to know
// gathering finished.
type SignalCandidate struct {
	Type      string          `json:"type"`
	SessionID string          `json:"session_id"`
	Candidate json.RawMessage `json:"candidate"`
}

// NewSignalCandidate builds the forwarded candidate.
func NewSignalCandidate(sessionID string, candidate json.RawMessage) SignalCandidate {
	return SignalCandidate{Type: TypeSignalCandidate, SessionID: sessionID, Candidate: candidate}
}

// ---------------------------------------------------------------------------
// Wire vocabulary 1.2 server frames (SFU steer).
// ---------------------------------------------------------------------------

// EngineAnswer is the engine's SDP answer to a member's engine.offer. The
// browser treats it exactly like a peer answer (RTCPeerConnection.set
// RemoteDescription) — the SFU's ICE/DTLS details ride inside the SDP.
type EngineAnswer struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

// NewEngineAnswer builds the steer answer.
func NewEngineAnswer(sessionID, sdp string) EngineAnswer {
	return EngineAnswer{Type: TypeEngineAnswer, SessionID: sessionID, SDP: sdp}
}

// EngineTrackPublished informs one member that the OTHER member minted a
// track on the engine for this session. `participant` carries the
// publisher's engine-side participant id (which the gateway pins to the
// peer's connection id — opaque to the browser, stable for the session).
type EngineTrackPublished struct {
	Type        string `json:"type"`
	SessionID   string `json:"session_id"`
	Participant string `json:"participant"`
	Track       string `json:"track"`
	Kind        string `json:"kind"`
}

// NewEngineTrackPublished builds the steer publish notice.
func NewEngineTrackPublished(sessionID, participant, track, kind string) EngineTrackPublished {
	return EngineTrackPublished{Type: TypeEngineTrackPublished, SessionID: sessionID, Participant: participant, Track: track, Kind: kind}
}
