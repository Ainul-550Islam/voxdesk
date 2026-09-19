// Package backup verifies VoxDesk database dump integrity.
//
// LocalCheck is a byte-for-byte parity port of
// app/release/ops.py::verify_backup_integrity (Step 13): same statuses, same
// detail strings. Verify adds the authoritative "pg_restore --list" check from
// scripts/backup_verify.sh and, when pg_restore is unavailable, an offline
// PGDMP magic-header check (the "surpass" half of roadmap Phase 5): a file
// without the magic cannot pass pg_restore --list, so bad magic is FAIL even
// offline, while good magic without pg_restore stays NOT_RUN exactly as the
// Python pre-check reports.
package backup

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/voxdesk/ops/internal/status"
)

// pgDumpMagic is the magic header of a PostgreSQL custom-format dump.
const pgDumpMagic = "PGDMP"

// Magic verdicts reported by MagicCheck.
const (
	MagicOK        = "ok"
	MagicBad       = "bad"
	MagicUnchecked = "unchecked"
)

// maxStderrDetail caps the pg_restore output retained for failure evidence,
// so a pathological dump cannot blow up the evidence record.
const maxStderrDetail = 2048

// defaultTimeout bounds the authoritative pg_restore check when the caller
// passes a non-positive timeout.
const defaultTimeout = 120 * time.Second

// Local is the outcome of the offline pre-check.
type Local struct {
	Status string
	Detail string
	Size   int64 // -1 when the dump could not be statted.
}

// LocalCheck mirrors ops.verify_backup_integrity exactly: a missing, non-file,
// or empty dump is FAIL; anything else is NOT_RUN because the authoritative
// check needs pg_restore. Detail strings are byte-identical to the Python
// implementation (the path is echoed verbatim, never cleaned).
func LocalCheck(dumpPath string) Local {
	info, err := os.Stat(dumpPath)
	if err != nil {
		// Python's Path.exists() is False for every stat failure, so every
		// stat error maps to "missing" here as well.
		return Local{Status: status.StatusFail, Detail: "backup missing: " + dumpPath, Size: -1}
	}
	if !info.Mode().IsRegular() {
		return Local{Status: status.StatusFail, Detail: "backup is not a file: " + dumpPath, Size: -1}
	}
	if info.Size() == 0 {
		return Local{Status: status.StatusFail, Detail: "backup is empty: " + dumpPath, Size: -1}
	}
	return Local{
		Status: status.StatusNotRun,
		Detail: fmt.Sprintf(
			"backup present (%d bytes); authoritative integrity check runs via pg_restore when PostgreSQL is available",
			info.Size(),
		),
		Size: info.Size(),
	}
}

// MagicCheck reads the dump header offline. MagicOK means the file starts with
// the PostgreSQL custom-format magic; anything else (short file, wrong magic,
// unreadable file) is MagicBad. Callers must only invoke it after LocalCheck
// has established the dump is a non-empty regular file.
func MagicCheck(dumpPath string) (verdict string, detail string) {
	f, err := os.Open(dumpPath)
	if err != nil {
		return MagicBad, "cannot read dump header: " + err.Error()
	}
	defer f.Close()
	header := make([]byte, len(pgDumpMagic))
	n, err := io.ReadFull(f, header)
	if err != nil || n != len(pgDumpMagic) {
		return MagicBad, "dump too short to be a PostgreSQL custom-format archive"
	}
	if string(header) != pgDumpMagic {
		return MagicBad, "dump lacks the PostgreSQL custom-format magic (PGDMP)"
	}
	return MagicOK, "dump starts with the PostgreSQL custom-format magic (PGDMP)"
}

// Result is the machine-readable evidence for one verification. The JSON key
// order is stable (struct order) so release-gate consumers can diff it.
type Result struct {
	Status             string `json:"status"`
	Detail             string `json:"detail"`
	Dump               string `json:"dump"`
	SizeBytes          *int64 `json:"size_bytes"`
	PgRestoreAvailable bool   `json:"pg_restore_available"`
	PgRestorePath      string `json:"pg_restore_path"`
	Magic              string `json:"magic"`
	LocalDetail        string `json:"local_detail"`
}

// Verify runs the full verification: local pre-check (Python parity), then
// the authoritative pg_restore --list check when pg_restore is available,
// else the offline magic check. An explicit pgRestore path is used verbatim;
// an empty one resolves via PATH. A non-positive timeout selects the default.
func Verify(dumpPath string, pgRestore string, timeout time.Duration) Result {
	local := LocalCheck(dumpPath)
	res := Result{
		Dump:        dumpPath,
		Magic:       MagicUnchecked,
		LocalDetail: local.Detail,
	}
	if local.Size >= 0 {
		size := local.Size
		res.SizeBytes = &size
	}
	bin, available := resolvePgRestore(pgRestore)
	res.PgRestoreAvailable = available
	res.PgRestorePath = bin
	if local.Status == status.StatusFail {
		res.Status = status.StatusFail
		res.Detail = local.Detail
		return res
	}
	if available {
		if errText, ok := runPgRestoreList(bin, dumpPath, timeout); ok {
			res.Status = status.StatusPass
			res.Detail = fmt.Sprintf("%s is a readable custom-format dump (%d bytes)", dumpPath, local.Size)
		} else {
			res.Status = status.StatusFail
			res.Detail = fmt.Sprintf("pg_restore --list failed for %s: %s", dumpPath, errText)
		}
		return res
	}
	verdict, magicDetail := MagicCheck(dumpPath)
	res.Magic = verdict
	if verdict != MagicOK {
		res.Status = status.StatusFail
		res.Detail = "pg_restore is unavailable and offline check failed: " + magicDetail
		return res
	}
	res.Status = status.StatusNotRun
	res.Detail = local.Detail + "; PGDMP magic verified offline, authoritative pg_restore check still pending"
	return res
}

// resolvePgRestore returns the binary to execute. An explicit path is trusted
// verbatim (execution errors then surface as FAIL); otherwise PATH is probed
// and a miss means the offline path must be taken.
func resolvePgRestore(pgRestore string) (string, bool) {
	if pgRestore != "" {
		return pgRestore, true
	}
	bin, err := exec.LookPath("pg_restore")
	if err != nil {
		return "", false
	}
	return bin, true
}

// runPgRestoreList executes "pg_restore --list" exactly as
// scripts/backup_verify.sh does. The (potentially huge) TOC on stdout is
// discarded; only a bounded stderr tail is kept for failure evidence.
func runPgRestoreList(bin, dumpPath string, timeout time.Duration) (string, bool) {
	if timeout <= 0 {
		timeout = defaultTimeout
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, "--list", dumpPath)
	cmd.Stdout = nil // discard: a dump TOC can be arbitrarily large
	var stderr bytes.Buffer
	cmd.Stderr = &cappedWriter{w: &stderr, max: maxStderrDetail}
	if err := cmd.Run(); err == nil {
		return "", true
	}
	text := strings.TrimSpace(stderr.String())
	if text == "" {
		text = "pg_restore exited non-zero with no output"
	}
	if ctx.Err() == context.DeadlineExceeded {
		text = "timed out: " + text
	}
	return firstLine(text), false
}

// cappedWriter passes through at most max bytes, discarding the rest.
type cappedWriter struct {
	w   io.Writer
	n   int
	max int
}

func (c *cappedWriter) Write(p []byte) (int, error) {
	orig := len(p)
	if c.n < c.max {
		if c.n+len(p) > c.max {
			p = p[:c.max-c.n]
		}
		m, err := c.w.Write(p)
		c.n += m
		if err != nil {
			return m, err
		}
	}
	return orig, nil
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
