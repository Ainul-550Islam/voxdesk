# services/ops — VoxDesk ops & resilience CLI (roadmap Phase 5)

Go ops tooling (stdlib only, single static binary) that produces the same
evidence the Python Step 13 verifiers produce, so the release gate can consume
either implementation. Each command documents its parity target explicitly.

## Commands

| Command | Parity target | Notes |
|---|---|---|
| `voxops backup-verify <dump>` | `app/release/ops.py::verify_backup_integrity` + `scripts/backup_verify.sh` | Offline `PGDMP` magic pre-check when `pg_restore` is absent (see below) |

Planned next (roadmap Phase 5 order): cost-report generator (parity with
`scripts/verify_cost_config.py`), migration runner, canary/deploy controller,
cert rotation + secret scanning.

## Parity contract: backup-verify

1. **Local pre-check** (`internal/backup.LocalCheck`) is a byte-for-byte port
   of `verify_backup_integrity`: same statuses (`FAIL` for missing / non-file /
   empty, else `NOT_RUN`), same detail strings, path echoed verbatim.
2. **Authoritative check** shells out to `pg_restore --list <dump>` exactly as
   `scripts/backup_verify.sh` does (`--list` output is discarded; only a
   bounded stderr tail is kept for failure evidence). Exit 0 → `PASS`,
   anything else → `FAIL`. Usage errors exit `2`, matching the shell script.
3. **Surpass (offline magic check):** when `pg_restore` is unavailable and the
   dump fails the `PGDMP` magic-header check, the result is `FAIL` — a file
   without the magic cannot pass `pg_restore --list`, so this matches the
   authoritative outcome without needing PostgreSQL. A good magic without
   `pg_restore` stays `NOT_RUN`, exactly as the Python pre-check reports.

Exit codes mirror `ops.exit_code_for_status`: `PASS` → 0, `FAIL` → 1,
`BLOCKED`/`NOT_RUN` → 2, `SKIPPED`/`NOT_APPLICABLE` → 0, anything else → 3.

## Usage

```sh
# Human output (OK line on stdout, anything else on stderr)
voxops backup-verify backups/voxdesk-20260914-120000.dump

# Machine-readable evidence (stable key order, suitable for the release gate)
voxops backup-verify --json backups/voxdesk-20260914-120000.dump

# Explicit pg_restore binary and timeout
voxops backup-verify --pg-restore /usr/lib/postgresql/16/bin/pg_restore \
  --timeout-seconds 300 backups/voxdesk-20260914-120000.dump
```

## Gates (same as CI `ops-go` job)

```sh
test -z "$(gofmt -l .)"   # format clean
go vet ./...              # vet clean
go test -race ./...       # race-detector tests
go build ./...            # builds
```

No third-party dependencies: `go build` works with `GOPROXY=off`.

## Toolchain note

The Go toolchain is installed per-phase outside the workspace (`/tmp/gotool`,
not persisted across sessions). Reinstall with:

```sh
curl -fsSL -o /tmp/go.tar.gz https://go.dev/dl/go1.27.1.linux-amd64.tar.gz
mkdir -p /tmp/gotool && tar -xzf /tmp/go.tar.gz -C /tmp/gotool
export PATH=/tmp/gotool/go/bin:$PATH
```
