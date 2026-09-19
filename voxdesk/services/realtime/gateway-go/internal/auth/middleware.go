package auth

import (
	"crypto/sha256"
	"crypto/subtle"
	"strings"
)

// Bearer-credential helpers shared by every HTTP surface on this edge that
// checks a shared secret (ingest, /metrics). They used to live inline in
// each handler and had started to drift — this file is the single
// implementation, so "how does the gateway compare secrets" has exactly one
// answer at review time.

// ExtractBearer splits an Authorization header of the form "Bearer <token>".
// A wrong scheme, an empty token or a missing header is simply not a
// credential; it is NOT an error worth distinguishing to the caller, whose
// only valid response is refusal either way.
func ExtractBearer(header string) (token string, ok bool) {
	token, ok = strings.CutPrefix(header, "Bearer ")
	if !ok || token == "" {
		return "", false
	}
	return token, true
}

// ConstantTimeTokenEqual compares a presented credential against the
// expected one. Both sides are hashed FIRST, so the comparison cost reveals
// nothing about the secret's length or content — the standard treatment for
// secrets that travel as HTTP headers, constant-time over the whole string.
func ConstantTimeTokenEqual(presented, expected string) bool {
	ph := sha256.Sum256([]byte(presented))
	eh := sha256.Sum256([]byte(expected))
	return subtle.ConstantTimeCompare(ph[:], eh[:]) == 1
}
