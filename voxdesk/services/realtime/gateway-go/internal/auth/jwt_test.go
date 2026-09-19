package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

// mint builds a compact JWS exactly the way the Python encoder does
// (app/auth/jwt.py: jwt.encode(payload, secret, algorithm="HS256")).
// Tests construct raw claim maps so each test can break exactly one
// property at a time.
func mint(t *testing.T, secret string, claims map[string]any, alg string) string {
	t.Helper()
	headerJSON, err := json.Marshal(map[string]any{"alg": alg, "typ": "JWT"})
	if err != nil {
		t.Fatalf("marshal header: %v", err)
	}
	claimsJSON, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("marshal claims: %v", err)
	}
	head := base64.RawURLEncoding.EncodeToString(headerJSON)
	body := base64.RawURLEncoding.EncodeToString(claimsJSON)
	input := head + "." + body
	if alg == "none" {
		return input + "."
	}
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(input))
	return input + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

const (
	testSecret   = "0123456789abcdef0123456789abcdef"
	testTenant   = "11111111-2222-3333-4444-555555555555"
	testUser     = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
	testAudience = "voxdesk-api"
	testIssuer   = "voxdesk"
)

// validClaims is the shape of a token the Python API would mint now.
func validClaims(now time.Time) map[string]any {
	return map[string]any{
		"sub":  testUser,
		"tid":  testTenant,
		"role": "owner",
		"tv":   float64(3),
		"typ":  "access",
		"iat":  float64(now.Unix()),
		"nbf":  float64(now.Unix()),
		"exp":  float64(now.Add(15 * time.Minute).Unix()),
		"iss":  testIssuer,
		"aud":  testAudience,
		"jti":  "random-jti",
	}
}

func newTestVerifier() *Verifier {
	return NewVerifier(testSecret, testIssuer, testAudience)
}

func TestValidTokenVerifies(t *testing.T) {
	now := time.Now()
	token := mint(t, testSecret, validClaims(now), "HS256")
	claims, err := newTestVerifier().Verify(token, now)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if claims.TenantID != testTenant || claims.UserID != testUser {
		t.Errorf("claims = %+v", claims)
	}
	if claims.Role != "owner" || claims.TokenVersion != 3 {
		t.Errorf("role/tv = %q/%d", claims.Role, claims.TokenVersion)
	}
	if !claims.ExpiresAt.After(now) {
		t.Errorf("ExpiresAt = %v, now = %v", claims.ExpiresAt, now)
	}
}

func TestAlgorithmPinned(t *testing.T) {
	now := time.Now()
	// alg=none: no signature at all must never be accepted.
	noneToken := mint(t, testSecret, validClaims(now), "none")
	if _, err := newTestVerifier().Verify(noneToken, now); !errors.Is(err, ErrMalformed) && !errors.Is(err, ErrAlgorithm) {
		t.Errorf("alg=none: err = %v, want ErrAlgorithm/ErrMalformed", err)
	}
	// A token lying about its algorithm name.
	rsToken := mint(t, testSecret, validClaims(now), "RS256")
	if _, err := newTestVerifier().Verify(rsToken, now); !errors.Is(err, ErrAlgorithm) {
		t.Errorf("alg=RS256: err = %v, want ErrAlgorithm", err)
	}
}

func TestWrongSecretRejected(t *testing.T) {
	now := time.Now()
	token := mint(t, "a-different-secret-0123456789abcdef", validClaims(now), "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrSignature) {
		t.Errorf("err = %v, want ErrSignature", err)
	}
}

func TestExpiredRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["exp"] = float64(now.Add(-10 * time.Minute).Unix())
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrExpired) {
		t.Errorf("err = %v, want ErrExpired", err)
	}
}

func TestNotBeforeFutureRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["nbf"] = float64(now.Add(5 * time.Minute).Unix())
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrNotYet) {
		t.Errorf("err = %v, want ErrNotYet", err)
	}
}

func TestWrongIssuerRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["iss"] = "somebody-else"
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrIssuer) {
		t.Errorf("err = %v, want ErrIssuer", err)
	}
}

func TestWrongAudienceRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["aud"] = "voxdesk-admin"
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrAudience) {
		t.Errorf("err = %v, want ErrAudience", err)
	}
}

func TestAudienceArrayAccepted(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["aud"] = []any{"other-api", testAudience} // JWT permits aud as a list
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); err != nil {
		t.Errorf("aud list containing the expected audience must verify: %v", err)
	}
}

func TestRefreshTypeRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["typ"] = "refresh" // a token minted for a different purpose
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrTokenType) {
		t.Errorf("err = %v, want ErrTokenType", err)
	}
}

func TestMissingClaimsRejected(t *testing.T) {
	now := time.Now()
	for _, drop := range []string{"sub", "tid", "role", "exp", "iat"} {
		claims := validClaims(now)
		delete(claims, drop)
		token := mint(t, testSecret, claims, "HS256")
		if _, err := newTestVerifier().Verify(token, now); err == nil {
			t.Errorf("claim %q dropped: expected rejection, got claims", drop)
		}
	}
}

func TestNonUUIDTenantRejected(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["tid"] = "tenant-one" // a human name, not a UUID: hub keying must never see it
	token := mint(t, testSecret, claims, "HS256")
	if _, err := newTestVerifier().Verify(token, now); !errors.Is(err, ErrBadSubject) {
		t.Errorf("err = %v, want ErrBadSubject", err)
	}
}

func TestTamperedPayloadRejected(t *testing.T) {
	now := time.Now()
	token := mint(t, testSecret, validClaims(now), "HS256")
	parts := strings.Split(token, ".")
	// Re-encode a DIFFERENT payload while keeping the original signature.
	claims := validClaims(now)
	claims["role"] = "platform_admin"
	tampered, _ := json.Marshal(claims)
	parts[1] = base64.RawURLEncoding.EncodeToString(tampered)
	if _, err := newTestVerifier().Verify(strings.Join(parts, "."), now); !errors.Is(err, ErrSignature) {
		t.Errorf("err = %v, want ErrSignature", err)
	}
}

func TestMalformedTokensRejected(t *testing.T) {
	now := time.Now()
	for _, token := range []string{"", "one", "one.two", "one.two.three.four", "..", "a.b.c"} {
		if _, err := newTestVerifier().Verify(token, now); err == nil {
			t.Errorf("token %q: expected rejection", token)
		}
	}
}

func TestExpiryWindowBoundary(t *testing.T) {
	now := time.Now()
	claims := validClaims(now)
	claims["exp"] = float64(now.Add(20 * time.Second).Unix())
	token := mint(t, testSecret, claims, "HS256")
	// A token 20 s from expiry still verifies; the clock leeway exists to
	// absorb NTP skew, not to gate this case.
	if _, err := newTestVerifier().Verify(token, now); err != nil {
		t.Errorf("token 20 s from expiry should verify, got %v", err)
	}
	// ...and 20 s PAST expiry (within the leeway) the same holds, mirroring
	// PyJWT leeway semantics for same-deployment clock jitter.
	if _, err := newTestVerifier().Verify(token, now.Add(25*time.Second)); err != nil {
		t.Errorf("inside leeway past expiry should verify, got %v", err)
	}
	// Beyond the leeway it must fail closed.
	if _, err := newTestVerifier().Verify(token, now.Add(2*time.Minute)); !errors.Is(err, ErrExpired) {
		t.Errorf("err = %v, want ErrExpired", err)
	}
}
