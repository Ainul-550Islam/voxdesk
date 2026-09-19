package config

import (
	"strings"
	"testing"
	"time"
)

// env builds a getenv func from a map (missing keys read as "").
func env(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

const (
	goodJWTSecret    = "0123456789abcdef0123456789abcdef"
	goodIngestSecret = "ingest-shared-secret-0123"
)

func validEnv() map[string]string {
	return map[string]string{
		"VOXDESK_GATEWAY_JWT_SECRET":    goodJWTSecret,
		"VOXDESK_GATEWAY_INGEST_SECRET": goodIngestSecret,
	}
}

func TestLoadDefaults(t *testing.T) {
	cfg, problems, _ := Load(env(validEnv()))
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if cfg.Port != DefaultPort {
		t.Errorf("Port = %d, want %d", cfg.Port, DefaultPort)
	}
	if cfg.MaxConnections != DefaultMaxConnections {
		t.Errorf("MaxConnections = %d, want %d", cfg.MaxConnections, DefaultMaxConnections)
	}
	if cfg.PingInterval != DefaultPingInterval || cfg.PongTimeout != DefaultPongTimeout {
		t.Errorf("heartbeat defaults changed: %v / %v", cfg.PingInterval, cfg.PongTimeout)
	}
	if cfg.JWTIssuer != "voxdesk" || cfg.JWTAudience != "voxdesk-api" {
		t.Errorf("JWT issuer/audience must default to the API's values, got %q/%q", cfg.JWTIssuer, cfg.JWTAudience)
	}
	// Unauthenticated surfaces default to WARNINGS, never silent acceptance.
	_ = cfg // warnings content checked in the dedicated tests below
}

func TestMissingJWTSecretRefusesToBoot(t *testing.T) {
	values := validEnv()
	delete(values, "VOXDESK_GATEWAY_JWT_SECRET")
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "VOXDESK_GATEWAY_JWT_SECRET is required") {
		t.Fatalf("expected required-secret problem, got %v", problems)
	}
}

func TestPlaceholderSecretsRefuseToBoot(t *testing.T) {
	for _, bad := range []string{"change-me", "insecure-development-only-change-me", "xxxx", "your-secret-here"} {
		values := validEnv()
		values["VOXDESK_GATEWAY_JWT_SECRET"] = bad
		_, problems, _ := Load(env(values))
		if !containsProblem(problems, "placeholder") {
			t.Errorf("jwt secret %q should be rejected as placeholder, got %v", bad, problems)
		}
	}
}

func TestShortJWTSecretRefusesToBoot(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_JWT_SECRET"] = "too-short"
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "at least 32 characters") {
		t.Fatalf("expected length problem, got %v", problems)
	}
}

func TestMissingIngestSecretRefusesToBoot(t *testing.T) {
	values := validEnv()
	delete(values, "VOXDESK_GATEWAY_INGEST_SECRET")
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "VOXDESK_GATEWAY_INGEST_SECRET is required") {
		t.Fatalf("expected ingest-required problem, got %v", problems)
	}
}

func TestInvalidPortRefusesToBoot(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_PORT"] = "0"
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "PORT") {
		t.Fatalf("expected port problem, got %v", problems)
	}
}

func TestPongTimeoutMustExceedPingInterval(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_PING_INTERVAL"] = "30s"
	values["VOXDESK_GATEWAY_PONG_TIMEOUT"] = "20s"
	_, problems, _ := Load(env(values))
	if !containsProblem(problems, "PONG_TIMEOUT") {
		t.Fatalf("expected heartbeat problem, got %v", problems)
	}
}

func TestDurationsAndOriginsParse(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_AUTH_TIMEOUT"] = "5s"
	values["VOXDESK_GATEWAY_ALLOWED_ORIGINS"] = " https://app.example.com ,https://staging.example.com "
	cfg, problems, _ := Load(env(values))
	if len(problems) != 0 {
		t.Fatalf("unexpected problems: %v", problems)
	}
	if cfg.AuthTimeout != 5*time.Second {
		t.Errorf("AuthTimeout = %v, want 5s", cfg.AuthTimeout)
	}
	if len(cfg.AllowedOrigins) != 2 || cfg.AllowedOrigins[0] != "https://app.example.com" {
		t.Errorf("AllowedOrigins = %v", cfg.AllowedOrigins)
	}
}

func TestEmptyMetricsTokenAndOriginsWarnNotFail(t *testing.T) {
	_, problems, warnings := Load(env(validEnv()))
	if len(problems) != 0 {
		t.Fatalf("unexpected problems: %v", problems)
	}
	if !containsProblem(warnings, "METRICS_TOKEN") {
		t.Errorf("expected metrics warning, got %v", warnings)
	}
	if !containsProblem(warnings, "ALLOWED_ORIGINS") {
		t.Errorf("expected origins warning, got %v", warnings)
	}
}

func containsProblem(list []string, needle string) bool {
	for _, item := range list {
		if strings.Contains(item, needle) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------- signaling relay knobs ---

func TestSignalingDefaultsApply(t *testing.T) {
	cfg, problems, _ := Load(env(validEnv()))
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if cfg.SignalingMaxSessionsPerTenant != DefaultSignalingMaxSessionsPerTenant {
		t.Errorf("SignalingMaxSessionsPerTenant = %d, want %d",
			cfg.SignalingMaxSessionsPerTenant, DefaultSignalingMaxSessionsPerTenant)
	}
	if cfg.SignalingPendingTimeout != DefaultSignalingPendingTimeout {
		t.Errorf("SignalingPendingTimeout = %v, want %v",
			cfg.SignalingPendingTimeout, DefaultSignalingPendingTimeout)
	}
}

func TestSignalingOverridesParse(t *testing.T) {
	values := validEnv()
	values["VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT"] = "128"
	values["VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT"] = "2m30s"
	cfg, problems, _ := Load(env(values))
	if len(problems) != 0 {
		t.Fatalf("expected no problems, got %v", problems)
	}
	if cfg.SignalingMaxSessionsPerTenant != 128 {
		t.Errorf("override = %d", cfg.SignalingMaxSessionsPerTenant)
	}
	if cfg.SignalingPendingTimeout != 150*time.Second {
		t.Errorf("timeout override = %v", cfg.SignalingPendingTimeout)
	}
}

func TestInvalidSignalingKnobsRefuseToBoot(t *testing.T) {
	for name, mutate := range map[string]func(map[string]string){
		"zero sessions per tenant": func(v map[string]string) {
			v["VOXDESK_GATEWAY_SIGNAL_MAX_SESSIONS_PER_TENANT"] = "0"
		},
		"zero pending timeout": func(v map[string]string) {
			v["VOXDESK_GATEWAY_SIGNAL_PENDING_TIMEOUT"] = "0s"
		},
	} {
		values := validEnv()
		mutate(values)
		_, problems, _ := Load(env(values))
		if len(problems) == 0 {
			t.Errorf("%s must refuse to boot", name)
		}
	}
}
