// Package auth verifies the dashboard access tokens the public edge trusts,
// and houses the shared credential-comparison helpers every secret-bearing
// HTTP surface on the edge uses.
//
// Verification (this file plus claims.go):
//
//   - the algorithm is pinned to HS256 — a token claiming alg "none" or an
//     asymmetric algorithm is rejected before the signature is even checked,
//     closing the classic alg-confusion downgrade;
//   - issuer and audience must match exactly;
//   - exp and nbf are enforced with a small clock leeway (the gateway and the
//     API run in the same deployment, so 30 s of skew tolerance costs nothing
//     and absorbs NTP jitter);
//   - typ must be "access" so a token minted for a different purpose can
//     never open a realtime session.
//
// What the gateway deliberately does NOT do: it does not consult the
// database, so User.token_version revocation is out of its reach. Access
// tokens live 15 minutes by design (ACCESS_TOKEN_MINUTES); the realtime edge
// inherits that same bound by closing the connection shortly after the
// presented token's exp (see heartbeat.go), forcing a reconnect with the
// fresh token the dashboard already rotates on schedule. That is the same
// trust window the dashboard UI itself operates under.
//
// File split: jwt.go = envelope (segments, signature, JOSE header pin),
// claims.go = payload (claim shapes, coercions, trusted Claims),
// middleware.go = shared Bearer/constant-time helpers.
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// clockLeeway covers same-deployment NTP jitter on exp/nbf/iat checks.
const clockLeeway = 30 * time.Second

// Sentinel errors. Callers map them to wire codes; the distinct values keep
// "why did authentication fail" out of the client-visible message (the client
// always gets the generic auth_failed) while staying precise in logs/tests.
var (
	ErrMalformed  = errors.New("token is not a compact JWS")
	ErrAlgorithm  = errors.New("token algorithm is not HS256")
	ErrSignature  = errors.New("signature verification failed")
	ErrExpired    = errors.New("token has expired")
	ErrNotYet     = errors.New("token is not yet valid")
	ErrIssuer     = errors.New("unexpected issuer")
	ErrAudience   = errors.New("unexpected audience")
	ErrTokenType  = errors.New("not an access token")
	ErrClaims     = errors.New("claims are missing or malformed")
	ErrBadSubject = errors.New("subject or tenant id is not a UUID")
)

// header is the JOSE header. Only alg is consulted, and only to be pinned.
type header struct {
	Algorithm string `json:"alg"`
}

// Verifier checks tokens against one shared secret and the expected
// issuer/audience. It is immutable and safe for concurrent use.
type Verifier struct {
	secret   []byte
	issuer   string
	audience string
}

// NewVerifier returns a Verifier. The secret must already have passed
// config validation (never empty, never a placeholder).
func NewVerifier(secret, issuer, audience string) *Verifier {
	return &Verifier{secret: []byte(secret), issuer: issuer, audience: audience}
}

// Verify parses and validates token, returning the trusted Claims or one of
// the sentinel errors. now is injectable so expiry behaviour is testable
// without sleeping.
func (v *Verifier) Verify(token string, now time.Time) (*Claims, error) {
	segments := strings.Split(token, ".")
	if len(segments) != 3 || segments[0] == "" || segments[1] == "" {
		return nil, ErrMalformed
	}

	headerBytes, err := base64.RawURLEncoding.DecodeString(segments[0])
	if err != nil {
		return nil, ErrMalformed
	}
	var hdr header
	if err := json.Unmarshal(headerBytes, &hdr); err != nil {
		return nil, ErrMalformed
	}
	// Pinned BEFORE the signature check: a token advertising an unexpected
	// algorithm never reaches the key, so there is no cross-algorithm
	// confusion surface to defend further.
	if hdr.Algorithm != "HS256" {
		return nil, ErrAlgorithm
	}

	signingInput := segments[0] + "." + segments[1]
	signature, err := base64.RawURLEncoding.DecodeString(segments[2])
	if err != nil {
		return nil, ErrMalformed
	}
	mac := hmac.New(sha256.New, v.secret)
	_, _ = mac.Write([]byte(signingInput))
	if !hmac.Equal(mac.Sum(nil), signature) {
		return nil, ErrSignature
	}

	payloadBytes, err := base64.RawURLEncoding.DecodeString(segments[1])
	if err != nil {
		return nil, ErrMalformed
	}
	var p payload
	if err := json.Unmarshal(payloadBytes, &p); err != nil {
		return nil, ErrMalformed
	}

	return validatePayload(&p, v.issuer, v.audience, now)
}
