// Package websocket owns the per-connection lifecycle of the public edge:
// upgrade, authentication, reading, writing, heartbeat, and close.
//
// The package is layered deliberately:
//
//	upgrader.go   — HTTP → WebSocket, origin policy, capacity gate
//	connection.go — the session object and its orchestration
//	reader.go     — inbound frame loop, dispatch, auth, rate limiting
//	writer.go     — the ONLY goroutine that writes data frames
//	heartbeat.go  — WS-level ping/pong deadlines, token-expiry enforcement
//	close.go      — idempotent, code-correct session termination
//
// Concurrency contract (gorilla's, which this package relies on exactly):
// one goroutine may read, one may write, and Close/WriteControl may run
// concurrently with anything. The writer goroutine owns ALL data frames;
// the heartbeat and close paths use WriteControl only.
package websocket

import (
	"net/http"
	"strings"
	"sync/atomic"

	gorilla "github.com/gorilla/websocket"

	"github.com/voxdesk/realtime/gateway-go/internal/config"
)

// upgraderCounter assigns a cheap monotonically increasing number to every
// upgrade attempt; it exists only so refusal logs can be correlated with the
// metric bump when several upgrades fail in the same second.
var upgraderCounter atomic.Uint64

// NewUpgrader builds the shared gorilla Upgrader (safe for concurrent use).
//
// Origin policy: browsers send Origin; non-browser clients commonly do not.
// The token is the actual credential, so an empty origin list must not
// accidentally allow every web page on the internet to hold a user's socket
// (CSWSH — a malicious origin riding the ambient... no, there is no ambient
// credential here, the token is explicit; but a permissive origin check
// would still let any site that obtains a token keep it alive). Fail
// closed: with no allowlist, only origin-less (same-host tooling) requests
// pass, mirroring the internal hub's "the edge checks origins" division of
// labour — except here WE are the edge, so the check lives in this file.
func NewUpgrader(cfg config.Config) gorilla.Upgrader {
	allowed := make(map[string]struct{}, len(cfg.AllowedOrigins))
	for _, origin := range cfg.AllowedOrigins {
		allowed[strings.ToLower(origin)] = struct{}{}
	}
	return gorilla.Upgrader{
		ReadBufferSize:  4096,
		WriteBufferSize: 4096,
		CheckOrigin: func(r *http.Request) bool {
			origin := r.Header.Get("Origin")
			if origin == "" {
				// Not a browser cross-origin request (server tooling,
				// curl, same-origin fetch from the served dashboard).
				return true
			}
			_, ok := allowed[strings.ToLower(origin)]
			return ok
		},
		// Never offer subprotocols: auth travels in the first JSON frame
		// (hello), not in Sec-WebSocket-Protocol, so there is exactly one
		// credential path to review.
		Subprotocols: nil,
		Error: func(w http.ResponseWriter, _ *http.Request, status int, _ error) {
			// Uniform refusal shape — no reason text, so the upgrade path
			// never leaks which policy (origin vs. capacity) fired.
			http.Error(w, http.StatusText(status), status)
		},
	}
}

// attemptNumber returns this process's next upgrade-attempt ordinal.
func attemptNumber() uint64 {
	return upgraderCounter.Add(1)
}
