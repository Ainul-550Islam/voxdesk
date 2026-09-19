// Package validate holds the small, shared shape-checks used across the
// gateway: UUIDs, room names, event kinds and placeholder secrets.
//
// These checks exist in exactly one place for the same reason the Python
// backend keeps one `verify_twilio_request`: a validation rule that is
// written twice will eventually be enforced in one place and not the other.
package validate

import "strings"

// IsUUID reports whether s is a canonical RFC 4122 UUID (8-4-4-4-12, hex,
// either case). The gateway never generates these — it only needs to know
// that a tenant id / call id / event id coming off the wire is the shape the
// Python backend persists, so a malformed id can never become a routing key
// or a log-confusable string.
func IsUUID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i := 0; i < 36; i++ {
		switch i {
		case 8, 13, 18, 23:
			if s[i] != '-' {
				return false
			}
		default:
			c := s[i]
			if !('0' <= c && c <= '9' || 'a' <= c && c <= 'f' || 'A' <= c && c <= 'F') {
				return false
			}
		}
	}
	return true
}

// Room namespaces the public edge will ever fan out to. This is a closed
// set on purpose: an open "subscribe to anything" surface would let a
// browser enumerate internal feed names. A dashboard subscribes to the
// tenant-wide feeds ("calls", "metrics") or to exactly one entity feed
// ("call:<uuid>", "campaign:<uuid>"). Anything else is room_invalid.
const (
	RoomCalls      = "calls"
	RoomMetrics    = "metrics"
	RoomCallPrefix = "call:"
	RoomCampPrefix = "campaign:"
)

// IsRoomName reports whether room is a public, well-formed room name.
func IsRoomName(room string) bool {
	switch room {
	case RoomCalls, RoomMetrics:
		return true
	}
	for _, prefix := range []string{RoomCallPrefix, RoomCampPrefix} {
		if strings.HasPrefix(room, prefix) {
			return IsUUID(room[len(prefix):])
		}
	}
	return false
}

// IsEventKind reports whether kind is a dotted event name such as
// "call.updated" or "transcript.turn". Kinds travel inside delivery frames
// and let the dashboard dispatch without parsing payloads; keeping them
// lower-snake-dotted stops a publisher from smuggling markup or control
// characters into a client's dispatch table.
func IsEventKind(kind string) bool {
	if len(kind) < 3 || len(kind) > 64 {
		return false
	}
	parts := strings.Split(kind, ".")
	if len(parts) < 2 {
		return false
	}
	for _, p := range parts {
		if p == "" {
			return false
		}
		for i := 0; i < len(p); i++ {
			c := p[i]
			if !('a' <= c && c <= 'z' || '0' <= c && c <= '9' || c == '_' || c == '-') {
				return false
			}
		}
	}
	return true
}

// placeholderMarkers mirrors app/core/config.py: a value containing any of
// these substrings is an example, not a credential, and the service must
// refuse to boot with it rather than "secure" a public edge with a string
// that is published in the repository.
var placeholderMarkers = []string{"change-me", "change_me", "insecure", "xxxx", "placeholder", "your-"}

// LooksPlaceholder reports whether value is obviously an unset example.
func LooksPlaceholder(value string) bool {
	lowered := strings.ToLower(value)
	if lowered == "" {
		return false
	}
	for _, marker := range placeholderMarkers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}
