package auth

// This file owns the PAYLOAD side of a dashboard access token: claim names,
// their coercions from Go's loose JSON typing, and what a verified payload
// becomes for the rest of the gateway. jwt.go owns the envelope (signature,
// algorithm pinning); this file is what the envelope is guarding.
//
// The token format is defined by app/auth/jwt.py on the Python side:
//
//	HS256, claims sub (user uuid), tid (tenant uuid), role, tv (token
//	version), typ="access", iat, nbf, exp, iss, aud, jti.

import (
	"fmt"
	"strings"
	"time"

	"github.com/voxdesk/realtime/gateway-go/internal/validate"
)

// Claims is the verified identity the rest of the gateway acts on. TenantID
// is THE security-critical field: every subscription and every delivery is
// keyed by it, and it only ever comes from a verified token.
type Claims struct {
	UserID       string
	TenantID     string
	Role         string
	TokenVersion int
	ExpiresAt    time.Time
	JTI          string
}

// payload mirrors the Python encoder's claim names.
type payload struct {
	Subject     string `json:"sub"`
	TenantID    string `json:"tid"`
	Role        string `json:"role"`
	TokenVer    any    `json:"tv"`
	Type        string `json:"typ"`
	IssuedAt    any    `json:"iat"`
	NotBefore   any    `json:"nbf"`
	ExpiresAt   any    `json:"exp"`
	Issuer      string `json:"iss"`
	AudienceRaw any    `json:"aud"`
	JTI         string `json:"jti"`
}

// validatePayload turns a decoded payload into trusted Claims, applying
// every check the Python decoder applies, in the same order: shape first
// (missing/mformed claims), then purpose/issuer/audience, then times, then
// the UUID shapes the rest of the system keys state by.
func validatePayload(p *payload, issuer, audience string, now time.Time) (*Claims, error) {
	if p.Subject == "" || p.TenantID == "" || p.Role == "" {
		return nil, ErrClaims
	}
	if p.Type != "access" {
		return nil, ErrTokenType
	}
	if p.Issuer != issuer {
		return nil, ErrIssuer
	}
	if !audienceMatches(p.AudienceRaw, audience) {
		return nil, ErrAudience
	}

	exp, ok := unixSeconds(p.ExpiresAt)
	if !ok {
		return nil, ErrClaims
	}
	if !now.Before(time.Unix(exp, 0).Add(clockLeeway)) {
		return nil, ErrExpired
	}
	if iat, ok := unixSeconds(p.IssuedAt); !ok {
		// iat is a required claim on the Python encoder (verify requires it),
		// so its absence here marks a token from a different mint entirely.
		return nil, ErrClaims
	} else if time.Unix(iat, 0).After(now.Add(clockLeeway)) {
		return nil, ErrNotYet
	}
	if nbf, ok := unixSeconds(p.NotBefore); ok {
		if time.Unix(nbf, 0).After(now.Add(clockLeeway)) {
			return nil, ErrNotYet
		}
	}

	// The Python decoder coerces sub/tid through uuid.UUID, which both
	// validates them and normalises casing. A gateway-side token with a
	// non-UUID tenant would break hub keying, so the same shape is required.
	if !validate.IsUUID(p.Subject) || !validate.IsUUID(p.TenantID) {
		return nil, ErrBadSubject
	}

	return &Claims{
		UserID:       strings.ToLower(p.Subject),
		TenantID:     strings.ToLower(p.TenantID),
		Role:         p.Role,
		TokenVersion: intVersion(p.TokenVer),
		ExpiresAt:    time.Unix(exp, 0),
		JTI:          p.JTI,
	}, nil
}

// audienceMatches accepts the two shapes JWT permits for aud: a bare string
// or an array of strings.
func audienceMatches(raw any, expected string) bool {
	switch aud := raw.(type) {
	case string:
		return aud == expected
	case []any:
		for _, item := range aud {
			if s, ok := item.(string); ok && s == expected {
				return true
			}
		}
	}
	return false
}

// unixSeconds coerces a JSON numeric claim (float64 after decoding) to
// seconds. Strings and other shapes are rejected: the Python encoder emits
// ints, and accepting strings would only widen the accepted-grammar for no
// operational reason.
func unixSeconds(v any) (int64, bool) {
	n, ok := v.(float64)
	if !ok {
		return 0, false
	}
	return int64(n), true
}

// intVersion tolerates tv arriving as a JSON number or a numeric string,
// mirroring int(payload.get("tv", 0)) on the Python side.
func intVersion(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case string:
		var i int
		if _, err := fmt.Sscanf(n, "%d", &i); err == nil {
			return i
		}
	}
	return 0
}
