// Command voxops is the VoxDesk ops & resilience CLI (roadmap Phase 5).
//
// Each subcommand documents its Python parity target: the Go tool must produce
// the same statuses, exit codes, and evidence the Step 13 verifiers produce,
// so the release gate can consume either implementation.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/voxdesk/ops/internal/backup"
	"github.com/voxdesk/ops/internal/status"
)

const usage = `voxops — VoxDesk ops & resilience CLI

Usage:
  voxops backup-verify [--json] [--pg-restore PATH] [--timeout-seconds N] <dump-file>

Commands:
  backup-verify   verify a database dump (parity with scripts/backup_verify.sh)

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED/USAGE, 3 = invalid configuration.
`

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run executes the CLI; it returns the process exit code instead of exiting so
// tests can drive every path in-process.
func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return status.ExitBlocked // 2, same as backup_verify.sh usage exit
	}
	switch args[0] {
	case "backup-verify":
		return runBackupVerify(args[1:], stdout, stderr)
	case "-h", "--help", "help":
		fmt.Fprint(stdout, usage)
		return status.ExitOK
	default:
		fmt.Fprintf(stderr, "unknown command: %s\n%s", args[0], usage)
		return status.ExitBlocked
	}
}

func runBackupVerify(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("backup-verify", flag.ContinueOnError)
	fs.SetOutput(stderr)
	asJSON := fs.Bool("json", false, "emit machine-readable evidence JSON")
	pgRestore := fs.String("pg-restore", "", "pg_restore binary (default: PATH lookup)")
	timeoutSecs := fs.Int("timeout-seconds", 120, "pg_restore timeout in seconds")
	if err := fs.Parse(args); err != nil {
		return status.ExitBlocked
	}
	if *timeoutSecs <= 0 {
		fmt.Fprintln(stderr, "timeout-seconds must be positive")
		return status.ExitConfig
	}
	if fs.NArg() != 1 {
		fmt.Fprintln(stderr, "usage: voxops backup-verify [--json] [--pg-restore PATH] [--timeout-seconds N] <dump-file>")
		return status.ExitBlocked
	}
	res := backup.Verify(fs.Arg(0), *pgRestore, time.Duration(*timeoutSecs)*time.Second)
	if *asJSON {
		out, err := json.MarshalIndent(res, "", "  ")
		if err != nil {
			fmt.Fprintln(stderr, "cannot encode evidence: "+err.Error())
			return status.ExitConfig
		}
		fmt.Fprintln(stdout, string(out))
	} else if res.Status == status.StatusPass {
		fmt.Fprintf(stdout, "OK: %s\n", res.Detail)
	} else {
		fmt.Fprintf(stderr, "%s: %s\n", res.Status, res.Detail)
	}
	return status.ExitCodeForStatus(res.Status)
}
