package protocol

// Error codes. The set is closed on purpose (mirrors the Python code's
// closed enums): clients can switch on it, and an open set would invite
// ad-hoc codes that leak internal state. TestErrorCodesStayClosed is the
// tripwire: extend the vocabulary here and it fails loudly until the
// dashboard-side handling is reviewed.
const (
	CodeBadMessage    = "bad_message"
	CodeHelloRequired = "hello_required"
	CodeAuthFailed    = "auth_failed"
	CodeRoomInvalid   = "room_invalid"
	CodeOverLimit     = "over_limit"
	CodeRateLimited   = "rate_limited"

	// Signaling failures. Three of them deliberately COLLAPSE what the
	// session layer knows — a non-member probing a session id, an id that
	// never existed, and an id belonging to another tenant all produce
	// session_unknown, because the distribution of which mkts exist is
	// itself tenant data. Accuracy in the log, ambiguity on the wire.
	CodeSessionUnknown = "session_unknown"
	// session_full: the tenant is at its concurrent-session cap.
	CodeSessionFull = "session_full"
	// session_not_ready: the action needs both members (an offer with no
	// peer yet) and the peer slot is empty.
	CodeSessionNotReady = "session_not_ready"
	// already_in_session: this connection already holds a session slot;
	// one connection, at most one session, so dashboards cannot accumulate
	// orphaned negotiations across tabs.
	CodeAlreadyInSession = "already_in_session"
	// wrong_signal_state: legal type, illegal moment — a second offer while
	// one is outstanding (glare), an answer with nothing to answer, any
	// signaling frame after the session ended.
	CodeWrongSignalState = "wrong_signal_state"
	// steer_mode_blocked: a frame addressed the wrong media path for the
	// session's negotiated mode — engine.* on a P2P session, or P2P
	// offer/answer/candidate on a steered one.
	CodeSteerModeBlocked = "steer_mode_blocked"
	// engine_unavailable: an engine.* frame while the media path is
	// unreachable or the connection's engine session was never enrolled.
	// Session-preserving by contract; retry once availability returns.
	CodeEngineUnavailable = "engine_unavailable"
	// signal_too_large: an SDP or candidate beyond the relay caps. Distinct
	// from bad_message so a client can tell "malformed" from "legitimate
	// but oversized" (an SDP with an extra codec block is the second).
	CodeSignalTooLarge = "signal_too_large"
)

// RFC 6455 close codes the gateway uses, re-exported so transport-agnostic
// packages (the hub) can request a close without importing gorilla.
const (
	CloseNormal          = 1000 // orderly end of session
	CloseGoingAway       = 1001 // server shutting down
	ClosePolicyViolation = 1008 // auth failed / timed out, room refused
	CloseInternalError   = 1011 // unexpected server-side failure
)

// ErrorMessage is the server's refusal/validation frame. The text is written
// for the dashboard developer, never for the caller: it must not echo tokens
// or tenant data.
type ErrorMessage struct {
	Type    string `json:"type"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// NewError builds an error frame.
func NewError(code, message string) ErrorMessage {
	return ErrorMessage{Type: TypeError, Code: code, Message: message}
}
