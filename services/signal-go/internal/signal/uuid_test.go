package signal

import (
	"strings"
	"testing"
)

func TestUUIDv4FormatAndVersion(t *testing.T) {
	for i := 0; i < 1000; i++ {
		id, err := NewUUIDv4()
		if err != nil {
			t.Fatalf("generate: %v", err)
		}
		// 8-4-4-4-12 lowercase hex.
		parts := strings.Split(id, "-")
		if len(parts) != 5 {
			t.Fatalf("uuid %q has %d parts", id, len(parts))
		}
		lens := []int{8, 4, 4, 4, 12}
		for p, n := range lens {
			if len(parts[p]) != n {
				t.Fatalf("uuid %q part %d wrong length", id, p)
			}
		}
		// Version nibble must be 4.
		if parts[2][0] != '4' {
			t.Fatalf("uuid %q is not version 4", id)
		}
		// Variant nibble must be 8, 9, a or b (10xx).
		v := parts[3][0]
		if v != '8' && v != '9' && v != 'a' && v != 'b' {
			t.Fatalf("uuid %q has invalid variant nibble %c", id, v)
		}
	}
}

func TestUUIDv4Uniqueness(t *testing.T) {
	seen := make(map[string]struct{}, 10000)
	for i := 0; i < 10000; i++ {
		id, err := NewUUIDv4()
		if err != nil {
			t.Fatalf("generate: %v", err)
		}
		if _, dup := seen[id]; dup {
			t.Fatalf("duplicate uuid %q", id)
		}
		seen[id] = struct{}{}
	}
}
