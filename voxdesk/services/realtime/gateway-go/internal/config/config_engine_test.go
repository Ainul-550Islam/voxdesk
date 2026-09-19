package config

import (
	"strings"
	"testing"
)

// Engine-plane configuration validation: explicit-or-absent discipline,
// same fail-closed posture as the broker switch — a half-typed engine URL
// must refuse loudly, never silently degrade.
func baseEnv() map[string]string {
	return map[string]string{
		"VOXDESK_GATEWAY_JWT_SECRET":    strings.Repeat("a", 40),
		"VOXDESK_GATEWAY_INGEST_SECRET": strings.Repeat("b", 20),
	}
}

func getenvFrom(m map[string]string) func(string) string {
	return func(key string) string { return m[key] }
}

func problemMentioning(problems []string, needle string) bool {
	for _, p := range problems {
		if strings.Contains(p, needle) {
			return true
		}
	}
	return false
}

func TestEngineURLAbsentDisablesPlaneWithWarning(t *testing.T) {
	cfg, problems, warnings := Load(getenvFrom(baseEnv()))
	if len(problems) > 0 {
		t.Fatalf("unexpected problems: %v", problems)
	}
	if cfg.MediaEngineURL != "" {
		t.Fatalf("cfg: %+v", cfg)
	}
	found := false
	for _, w := range warnings {
		if strings.Contains(w, "MEDIA_ENGINE_URL is empty") {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected the disabled-plane warning, got %v", warnings)
	}
}

func TestEngineURLSchemeRequired(t *testing.T) {
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "media-engine-rs:9001"
	_, problems, _ := Load(getenvFrom(env))
	if !problemMentioning(problems, "http://") {
		t.Fatalf("scheme must be demanded: %v", problems)
	}
}

func TestEngineTimeoutBounds(t *testing.T) {
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine-rs:9001"
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS"] = "0"
	if _, problems, _ := Load(getenvFrom(env)); !problemMentioning(problems, "TIMEOUT") {
		t.Fatalf("zero timeout must refuse: %v", problems)
	}
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS"] = "100"
	if _, problems, _ := Load(getenvFrom(env)); !problemMentioning(problems, "TIMEOUT") {
		t.Fatalf("absurd timeout must refuse")
	}
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_TIMEOUT_SECONDS"] = "1.5"
	cfg, problems, _ := Load(getenvFrom(env))
	if len(problems) > 0 || cfg.MediaEngineTimeoutSeconds != 1.5 {
		t.Fatalf("valid engine config: problems=%v cfg=%+v", problems, cfg)
	}
}

func TestEngineTimeoutDefaultApplies(t *testing.T) {
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine-rs:9001"
	cfg, problems, _ := Load(getenvFrom(env))
	if len(problems) > 0 {
		t.Fatalf("problems: %v", problems)
	}
	if cfg.MediaEngineTimeoutSeconds != DefaultMediaEngineTimeoutSeconds {
		t.Fatalf("default not applied: %v", cfg.MediaEngineTimeoutSeconds)
	}
}

// Steer-mode validation (closed vocabulary + engine pairing rules).
func TestSteerModeClosedVocabularyAndPairing(t *testing.T) {
	// default is off
	env := baseEnv()
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine:9001"
	cfg, problems, _ := Load(getenvFrom(env))
	if len(problems) > 0 || cfg.EngineSteer != EngineSteerOff {
		t.Fatalf("default steer: problems=%v steer=%q", problems, cfg.EngineSteer)
	}

	// unknown value refuses
	env["VOXDESK_GATEWAY_ENGINE_STEER"] = "sideways"
	_, problems, _ = Load(getenvFrom(env))
	if !problemMentioning(problems, "VOXDESK_GATEWAY_ENGINE_STEER") {
		t.Fatalf("unknown steer value must refuse: %v", problems)
	}

	// v1.2/force require a configured engine URL
	delete(env, "VOXDESK_GATEWAY_MEDIA_ENGINE_URL")
	for _, mode := range []string{EngineSteerV12, EngineSteerForce} {
		env["VOXDESK_GATEWAY_ENGINE_STEER"] = mode
		_, problems, _ = Load(getenvFrom(env))
		if !problemMentioning(problems, "MEDIA_ENGINE_URL") {
			t.Fatalf("steer=%s without engine URL must refuse: %v", mode, problems)
		}
	}

	// both modes accept with URL present
	env["VOXDESK_GATEWAY_MEDIA_ENGINE_URL"] = "http://media-engine:9001"
	for _, mode := range []string{EngineSteerV12, EngineSteerForce} {
		env["VOXDESK_GATEWAY_ENGINE_STEER"] = mode
		if _, problems, _ := Load(getenvFrom(env)); len(problems) > 0 {
			t.Fatalf("steer=%s with URL must boot: %v", mode, problems)
		}
	}
}
