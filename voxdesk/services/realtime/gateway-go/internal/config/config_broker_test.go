package config

import (
	"strings"
	"testing"
)

// goodEnv mirrors the minimal good environment config_test's helpers
// establish; kept locally so this file stays independent of the original
// test file's fixture helpers.
func goodEnv(extra map[string]string) func(string) string {
	env := map[string]string{
		"VOXDESK_GATEWAY_JWT_SECRET":    "jwt-secret-jwt-secret-jwt-secret-42",
		"VOXDESK_GATEWAY_INGEST_SECRET": "ingest-secret-ingest-secret",
	}
	for k, v := range extra {
		env[k] = v
	}
	return func(key string) string { return env[key] }
}

func TestBrokerDefaultsToMemory(t *testing.T) {
	t.Parallel()
	cfg, problems, _ := Load(goodEnv(nil))
	if len(problems) != 0 {
		t.Fatalf("good env must boot: %v", problems)
	}
	if cfg.BrokerKind != "memory" || cfg.RedisURL != "" {
		t.Fatalf("broker must default to the single-node memory broker, got %q/%q", cfg.BrokerKind, cfg.RedisURL)
	}
}

func TestBrokerRedisRequiresURL(t *testing.T) {
	t.Parallel()
	_, problems, _ := Load(goodEnv(map[string]string{"VOXDESK_GATEWAY_BROKER": "redis"}))
	if !mentions(problems, "REDIS_URL") {
		t.Fatalf("redis broker without a URL must refuse to boot: %v", problems)
	}
}

func TestBrokerRedisRejectsNonRedisScheme(t *testing.T) {
	t.Parallel()
	_, problems, _ := Load(goodEnv(map[string]string{
		"VOXDESK_GATEWAY_BROKER":    "redis",
		"VOXDESK_GATEWAY_REDIS_URL": "rediss://bus.internal:6379/0",
	}))
	if !mentions(problems, "redis://") {
		t.Fatalf("TLS/redis-scheme URLs must be refused explicitly: %v", problems)
	}
}

func TestBrokerKindIsCaseInsensitivelyUnknown(t *testing.T) {
	t.Parallel()
	_, problems, _ := Load(goodEnv(map[string]string{"VOXDESK_GATEWAY_BROKER": "Kafka"}))
	if !mentions(problems, "must be memory or redis") {
		t.Fatalf("an unknown broker must refuse to boot (a typo must not silently single-node): %v", problems)
	}
}

func TestBrokerRedisValidConfigBoots(t *testing.T) {
	t.Parallel()
	cfg, problems, warnings := Load(goodEnv(map[string]string{
		"VOXDESK_GATEWAY_BROKER":    "Redis",
		"VOXDESK_GATEWAY_REDIS_URL": "redis://:secret@127.0.0.1:6379/3",
	}))
	if len(problems) != 0 {
		t.Fatalf("valid redis config must boot: %v", problems)
	}
	if cfg.BrokerKind != "redis" || cfg.RedisURL != "redis://:secret@127.0.0.1:6379/3" {
		t.Fatalf("broker fields wrong: %+v", cfg)
	}
	if mentions(warnings, "REDIS_URL is set but") {
		t.Fatal("a USED redis URL must not warn")
	}
}

func TestMemoryBrokerWithRedisURLWarnsNotFails(t *testing.T) {
	t.Parallel()
	_, problems, warnings := Load(goodEnv(map[string]string{
		"VOXDESK_GATEWAY_REDIS_URL": "redis://127.0.0.1:6379",
	}))
	if len(problems) != 0 {
		t.Fatalf("a redundant URL must not block boot: %v", problems)
	}
	if !mentions(warnings, "ignored") {
		t.Fatalf("a redundant URL must WARN (it suggests operator intent): %v", warnings)
	}
}

// containsProblem reports whether any message mentions the marker.
func mentions(messages []string, marker string) bool {
	for _, m := range messages {
		if strings.Contains(m, marker) {
			return true
		}
	}
	return false
}
