package backup

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/voxdesk/ops/internal/status"
)

// writeDump creates a dump fixture with the given content.
func writeDump(t *testing.T, name, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// lookPath skips the test when the helper binary is unavailable.
func lookPath(t *testing.T, name string) string {
	t.Helper()
	bin, err := exec.LookPath(name)
	if err != nil {
		t.Skipf("%s not available: %v", name, err)
	}
	return bin
}

// isolatePATH removes every directory from PATH so pg_restore cannot resolve.
func isolatePATH(t *testing.T) {
	t.Helper()
	t.Setenv("PATH", t.TempDir())
}

func TestLocalCheckParity(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "nope.dump")
	subdir := filepath.Join(dir, "sub")
	if err := os.Mkdir(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	empty := writeDump(t, "empty.dump", "")
	present := writeDump(t, "present.dump", "PGDMP"+strings.Repeat("x", 95)) // exactly 100 bytes

	cases := []struct {
		name       string
		path       string
		wantStatus string
		wantDetail string
		wantSize   int64
	}{
		{"missing", missing, status.StatusFail, "backup missing: " + missing, -1},
		{"directory", subdir, status.StatusFail, "backup is not a file: " + subdir, -1},
		{"empty", empty, status.StatusFail, "backup is empty: " + empty, -1},
		{
			"present_is_not_run", present, status.StatusNotRun,
			"backup present (100 bytes); authoritative integrity check runs via pg_restore when PostgreSQL is available",
			100,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := LocalCheck(tc.path)
			if got.Status != tc.wantStatus || got.Detail != tc.wantDetail || got.Size != tc.wantSize {
				t.Fatalf("LocalCheck(%q) = %+v, want status=%q detail=%q size=%d",
					tc.path, got, tc.wantStatus, tc.wantDetail, tc.wantSize)
			}
		})
	}
}

func TestMagicCheck(t *testing.T) {
	good := writeDump(t, "good.dump", "PGDMP"+strings.Repeat("x", 95))
	bad := writeDump(t, "bad.dump", "SQLIT"+strings.Repeat("x", 95))
	short := writeDump(t, "short.dump", "PGD")
	empty := writeDump(t, "empty.dump", "")

	if verdict, _ := MagicCheck(good); verdict != MagicOK {
		t.Fatalf("good magic verdict = %q, want %q", verdict, MagicOK)
	}
	for name, path := range map[string]string{"bad": bad, "short": short, "empty": empty} {
		if verdict, _ := MagicCheck(path); verdict != MagicBad {
			t.Fatalf("%s verdict = %q, want %q", name, verdict, MagicBad)
		}
	}
	if verdict, _ := MagicCheck(filepath.Join(t.TempDir(), "missing.dump")); verdict != MagicBad {
		t.Fatalf("missing verdict = %q, want %q", verdict, MagicBad)
	}
}

func TestVerifyLocalFailureShortCircuits(t *testing.T) {
	res := Verify(filepath.Join(t.TempDir(), "missing.dump"), "", time.Minute)
	if res.Status != status.StatusFail {
		t.Fatalf("status = %q, want FAIL", res.Status)
	}
	if !strings.HasPrefix(res.Detail, "backup missing: ") {
		t.Fatalf("detail = %q, want missing prefix", res.Detail)
	}
	if res.Magic != MagicUnchecked {
		t.Fatalf("magic = %q, want unchecked", res.Magic)
	}
	if res.SizeBytes != nil {
		t.Fatalf("size_bytes = %d, want null", *res.SizeBytes)
	}
	if res.LocalDetail != res.Detail {
		t.Fatalf("local_detail = %q, want parity detail %q", res.LocalDetail, res.Detail)
	}
}

func TestVerifyAuthoritativePass(t *testing.T) {
	dump := writeDump(t, "d.dump", "PGDMP"+strings.Repeat("x", 95))
	res := Verify(dump, lookPath(t, "true"), time.Minute) // /bin/true: exit 0 like pg_restore success
	if res.Status != status.StatusPass {
		t.Fatalf("status = %q (%s), want PASS", res.Status, res.Detail)
	}
	if !strings.Contains(res.Detail, "is a readable custom-format dump (100 bytes)") {
		t.Fatalf("detail = %q, want readable-dump wording", res.Detail)
	}
	if !res.PgRestoreAvailable || res.Magic != MagicUnchecked {
		t.Fatalf("available=%v magic=%q, want true/unchecked", res.PgRestoreAvailable, res.Magic)
	}
	if res.SizeBytes == nil || *res.SizeBytes != 100 {
		t.Fatalf("size_bytes = %v, want 100", res.SizeBytes)
	}
}

func TestVerifyAuthoritativeFailure(t *testing.T) {
	dump := writeDump(t, "d.dump", "whatever")
	res := Verify(dump, lookPath(t, "false"), time.Minute) // /bin/false: exit 1 like pg_restore failure
	if res.Status != status.StatusFail {
		t.Fatalf("status = %q, want FAIL", res.Status)
	}
	if !strings.Contains(res.Detail, "pg_restore --list failed for ") {
		t.Fatalf("detail = %q, want pg_restore failure wording", res.Detail)
	}
}

func TestVerifyDefaultTimeoutIsAccepted(t *testing.T) {
	dump := writeDump(t, "d.dump", "whatever")
	res := Verify(dump, lookPath(t, "true"), 0) // non-positive timeout selects the default
	if res.Status != status.StatusPass {
		t.Fatalf("status = %q, want PASS", res.Status)
	}
}

func TestVerifyOfflineGoodMagicStaysNotRun(t *testing.T) {
	isolatePATH(t)
	dump := writeDump(t, "d.dump", "PGDMP"+strings.Repeat("x", 95))
	res := Verify(dump, "", time.Minute)
	if res.Status != status.StatusNotRun {
		t.Fatalf("status = %q, want NOT_RUN (Python parity)", res.Status)
	}
	if res.PgRestoreAvailable || res.PgRestorePath != "" {
		t.Fatalf("pg_restore resolved unexpectedly: %+v", res)
	}
	if res.Magic != MagicOK {
		t.Fatalf("magic = %q, want ok", res.Magic)
	}
	if !strings.Contains(res.Detail, "PGDMP magic verified offline") {
		t.Fatalf("Detail = %q, want offline-magic note", res.Detail)
	}
	// The local pre-check detail keeps exact Python parity untouched.
	if !strings.HasPrefix(res.LocalDetail, "backup present (100 bytes); authoritative integrity check") {
		t.Fatalf("local_detail = %q, want Python parity", res.LocalDetail)
	}
}

func TestVerifyOfflineBadMagicFails(t *testing.T) {
	isolatePATH(t)
	dump := writeDump(t, "d.dump", "plain text, definitely not a dump")
	res := Verify(dump, "", time.Minute)
	if res.Status != status.StatusFail {
		t.Fatalf("status = %q, want FAIL", res.Status)
	}
	if res.Magic != MagicBad {
		t.Fatalf("magic = %q, want bad", res.Magic)
	}
	if !strings.Contains(res.Detail, "offline check failed") {
		t.Fatalf("Detail = %q, want offline-failure wording", res.Detail)
	}
}
