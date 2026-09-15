package signal

import (
	"crypto/rand"
	"fmt"
)

// NewUUIDv4 returns a random (version 4) UUID per RFC 4122 §4.4, drawn from
// the CSPRNG. The Rust counterpart uses the `uuid` crate's v4 generator; the
// two produce byte-equivalent formatting (8-4-4-4-12 lowercase hex).
func NewUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant (10xx)
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
