package validate

import "testing"

func TestIsUUID(t *testing.T) {
	valid := []string{
		"550e8400-e29b-41d4-a716-446655440000",
		"550E8400-E29B-41D4-A716-446655440000", // upper case accepted (Python uuid.UUID normalises)
		"00000000-0000-0000-0000-000000000000",
	}
	for _, s := range valid {
		if !IsUUID(s) {
			t.Errorf("IsUUID(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"550e8400e29b41d4a716446655440000",      // no dashes
		"550e8400-e29b-41d4-a716-44665544000",   // too short
		"550e8400-e29b-41d4-a716-4466554400000", // too long
		"550e8400-e29b-41d4-a716-44665544000g",  // non-hex
		"550e8400_e29b_41d4_a716_446655440000",  // wrong separators
	}
	for _, s := range invalid {
		if IsUUID(s) {
			t.Errorf("IsUUID(%q) = true, want false", s)
		}
	}
}

func TestIsRoomName(t *testing.T) {
	valid := []string{
		"calls",
		"metrics",
		"call:550e8400-e29b-41d4-a716-446655440000",
		"campaign:550e8400-e29b-41d4-a716-446655440000",
	}
	for _, s := range valid {
		if !IsRoomName(s) {
			t.Errorf("IsRoomName(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"admin",           // not a public namespace
		"call:not-a-uuid", // malformed entity id
		"call:",           // empty entity id
		"tenant:550e8400-e29b-41d4-a716-446655440000", // tenant must come from the token, not the room
		"calls; DROP TABLE tenants",                   // injection-shaped
		"metrics ",                                    // trailing whitespace
	}
	for _, s := range invalid {
		if IsRoomName(s) {
			t.Errorf("IsRoomName(%q) = true, want false", s)
		}
	}
}

func TestIsEventKind(t *testing.T) {
	valid := []string{"call.updated", "transcript.turn", "metrics.tick_15s", "a.b.c-d_e"}
	for _, s := range valid {
		if !IsEventKind(s) {
			t.Errorf("IsEventKind(%q) = false, want true", s)
		}
	}
	invalid := []string{
		"",
		"noversion", // no dot
		"no..dots",
		".leading",
		"trailing.",
		"UPPER.CASE", // kinds are lower-snake by convention
		"<script>alert(1)</script>.x",
	}
	for _, s := range invalid {
		if IsEventKind(s) {
			t.Errorf("IsEventKind(%q) = true, want false", s)
		}
	}
}

func TestLooksPlaceholder(t *testing.T) {
	if !LooksPlaceholder("insecure-development-only-change-me") {
		t.Error("the Python default JWT secret must be detected as a placeholder")
	}
	if !LooksPlaceholder("XXXX-1234") {
		t.Error("xxxx marker should match case-insensitively")
	}
	if LooksPlaceholder("0123456789abcdef0123456789abcdef") {
		t.Error("a real hex secret must not be flagged")
	}
	if LooksPlaceholder("") {
		t.Error("empty is handled separately (required check), not as placeholder")
	}
}
