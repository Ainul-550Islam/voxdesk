package observability

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

// RequestIDHeader is the correlation id the Python API already sends on
// outbound calls (app/core/middleware stamps it inbound; the publisher
// worker forwards it on webhook-like POSTs). Honouring it here is what
// makes one HTTP request traceable API → gateway → browser delivery
// without any tracing library on either side.
const RequestIDHeader = "X-Request-ID"

// requestIDKey is the unexported context key; use RequestIDFrom to read.
type requestIDKey struct{}

// RequestIDMiddleware returns HTTP middleware that guarantees every
// request carries a correlation id:
//
//   - an inbound X-Request-ID (sane length, no control bytes) is kept —
//     that's the trace continuing;
//   - anything else gets a fresh random id.
//
// The id is stamped on the response (so the CALLER can correlate, even
// when it didn't send one) and stored in the request context for
// RequestIDFrom. WebSocket upgrades pass through it too: the id then
// belongs to the handshake, and per-frame correlation is the session id's
// job (see protocol.NewWelcome).
func RequestIDMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := sanitizeRequestID(r.Header.Get(RequestIDHeader))
		if id == "" {
			id = newRequestID()
		}
		w.Header().Set(RequestIDHeader, id)
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), requestIDKey{}, id)))
	})
}

// RequestIDFrom extracts the id the middleware stored ("" when absent —
// e.g. request paths constructed by hand in tests).
func RequestIDFrom(ctx context.Context) string {
	if id, ok := ctx.Value(requestIDKey{}).(string); ok {
		return id
	}
	return ""
}

// sanitizeRequestID keeps a presented id only when it is printable, short
// and control-byte-free. An echoed response header is a header-injection
// surface, so anything exotic is treated as absent and replaced.
func sanitizeRequestID(raw string) string {
	if len(raw) == 0 || len(raw) > 128 {
		return ""
	}
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if c < 0x20 || c > 0x7e {
			return ""
		}
	}
	return raw
}

// newRequestID mints 16 random bytes as hex — same 32-hex-char shape as a
// dashes-stripped UUID, so traces and session ids interleave readably.
func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing at this scale is a system failure; better a
		// distinctive constant that greps loudly than a crash in middleware.
		return "0000000000000000-rand-failed"
	}
	return hex.EncodeToString(b[:])
}
