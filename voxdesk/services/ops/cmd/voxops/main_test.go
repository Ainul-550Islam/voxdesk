package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

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

func TestRunBackupVerifyPassHuman(t *testing.T) {
	dump := writeDump(t, "d.dump", "PGDMP"+strings.Repeat("x", 95))
	var stdout, stderr bytes.Buffer
	code := run([]string{"backup-verify", "--pg-restore", lookPath(t, "true"), dump}, &stdout, &stderr)
	if code != status.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr: %s)", code, stderr.String())
	}
	want := "OK: " + dump + " is a readable custom-format dump (100 bytes)\n"
	if stdout.String() != want {
		t.Fatalf("stdout = %q, want %q", stdout.String(), want)
	}
}

func TestRunBackupVerifyFailHuman(t *testing.T) {
	dump := writeDump(t, "d.dump", "whatever")
	var stdout, stderr bytes.Buffer
	code := run([]string{"backup-verify", "--pg-restore", lookPath(t, "false"), dump}, &stdout, &stderr)
	if code != status.ExitFail {
		t.Fatalf("exit = %d, want 1", code)
	}
	if !strings.HasPrefix(stderr.String(), "FAIL: pg_restore --list failed for ") {
		t.Fatalf("stderr = %q, want FAIL wording", stderr.String())
	}
	if stdout.String() != "" {
		t.Fatalf("stdout = %q, want empty on failure", stdout.String())
	}
}

func TestRunBackupVerifyJSON(t *testing.T) {
	dump := writeDump(t, "d.dump", "PGDMP"+strings.Repeat("x", 95))
	var stdout, stderr bytes.Buffer
	code := run([]string{"backup-verify", "--json", "--pg-restore", lookPath(t, "true"), dump}, &stdout, &stderr)
	if code != status.ExitOK {
		t.Fatalf("exit = %d, want 0 (stderr: %s)", code, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "{\n  \"status\": \"PASS\",") {
		t.Fatalf("JSON does not start with stable status key: %q", stdout.String())
	}
	var decoded struct {
		Status             string `json:"status"`
		Detail             string `json:"detail"`
		Dump               string `json:"dump"`
		SizeBytes          *int64 `json:"size_bytes"`
		PgRestoreAvailable bool   `json:"pg_restore_available"`
		Magic              string `json:"magic"`
		LocalDetail        string `json:"local_detail"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("evidence is not valid JSON: %v", err)
	}
	if decoded.Status != status.StatusPass || decoded.Dump != dump {
		t.Fatalf("decoded = %+v, want PASS for %s", decoded, dump)
	}
	if decoded.SizeBytes == nil || *decoded.SizeBytes != 100 {
		t.Fatalf("size_bytes = %v, want 100", decoded.SizeBytes)
	}
	if !decoded.PgRestoreAvailable || decoded.Magic != "unchecked" {
		t.Fatalf("decoded = %+v, want available/unchecked", decoded)
	}
	if !strings.Contains(decoded.LocalDetail, "backup present (100 bytes)") {
		t.Fatalf("local_detail = %q, want Python parity", decoded.LocalDetail)
	}
}

func TestRunBackupVerifyOfflineNotRun(t *testing.T) {
	t.Setenv("PATH", t.TempDir()) // pg_restore cannot resolve
	dump := writeDump(t, "d.dump", "PGDMP"+strings.Repeat("x", 95))
	var stdout, stderr bytes.Buffer
	code := run([]string{"backup-verify", dump}, &stdout, &stderr)
	if code != status.ExitBlocked {
		t.Fatalf("exit = %d, want 2 (NOT_RUN)", code)
	}
	if !strings.HasPrefix(stderr.String(), "NOT_RUN: backup present (100 bytes);") {
		t.Fatalf("stderr = %q, want NOT_RUN parity detail", stderr.String())
	}
}

func TestRunUsageErrors(t *testing.T) {
	cases := []struct {
		name string
		args []string
	}{
		{"no_args", nil},
		{"unknown_command", []string{"frobnicate"}},
		{"missing_dump", []string{"backup-verify"}},
		{"extra_dump", []string{"backup-verify", "a.dump", "b.dump"}},
		{"bad_flag", []string{"backup-verify", "--bogus", "a.dump"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if code := run(tc.args, &stdout, &stderr); code != status.ExitBlocked {
				t.Fatalf("exit = %d, want 2", code)
			}
		})
	}
}

func TestRunBadTimeoutIsConfigError(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run([]string{"backup-verify", "--timeout-seconds", "0", "a.dump"}, &stdout, &stderr)
	if code != status.ExitConfig {
		t.Fatalf("exit = %d, want 3", code)
	}
}

func TestRunHelp(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"--help"}, &stdout, &stderr); code != status.ExitOK {
		t.Fatalf("exit = %d, want 0", code)
	}
	if !strings.Contains(stdout.String(), "backup-verify") {
		t.Fatalf("help = %q, want command list", stdout.String())
	}
}
