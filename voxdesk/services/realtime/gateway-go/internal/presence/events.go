package presence

import "time"

// Kind classifies a presence transition. The Online/Offline pair marks the
// moments a user gains or loses their FIRST/LAST live session (the answer
// "is this user reachable" flipped); the Session pair fires for additional
// tabs/devices joining or leaving without changing reachability.
type Kind int

const (
	// KindUnchanged is a no-op bookkeeping result (duplicate join,
	// unmatched leave). Never delivered to OnChange subscribers.
	KindUnchanged Kind = iota
	// KindUserOnline: the user's first live session appeared.
	KindUserOnline
	// KindUserOffline: the user's last live session is gone.
	KindUserOffline
	// KindSessionJoined: an additional session for an already-online user.
	KindSessionJoined
	// KindSessionLeft: one of several sessions left; user stays online.
	KindSessionLeft
)

// Event is one presence transition. UserSessions/TenantUsers/TotalUsers
// are snapshots taken AFTER the transition applied, so a subscriber can
// update instruments from the event alone without re-reading the registry
// (and without taking its lock).
type Event struct {
	Kind      Kind
	TenantID  string
	UserID    string
	SessionID string
	At        time.Time

	// UserSessions: live sessions of THIS user after the transition
	// (0 on KindUserOffline).
	UserSessions int
	// TenantUsers: reachable users of THIS tenant after the transition.
	TenantUsers int
	// TotalUsers: reachable (tenant, user) pairs node-wide — the gauge
	// value /metrics renders.
	TotalUsers int
}

// String renders the kind for logs.
func (k Kind) String() string {
	switch k {
	case KindUserOnline:
		return "user_online"
	case KindUserOffline:
		return "user_offline"
	case KindSessionJoined:
		return "session_joined"
	case KindSessionLeft:
		return "session_left"
	default:
		return "unchanged"
	}
}
