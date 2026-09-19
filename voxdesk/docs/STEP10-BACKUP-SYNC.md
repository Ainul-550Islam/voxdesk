# VoxDesk — Off-site backup & restore procedure (Step 10, item N)

## Status

**Static validation only.** The scripts were reviewed and are present and
correct, but this execution environment has no `pg_dump`, no `rclone`, no
`RCLONE_REMOTE` credential and no database, so **no connectivity test, no
sync, and no integrity run were performed**. The exact blockers:

* `pg_dump` / `pg_restore`: not installed in the sandbox.
* `rclone`: not installed.
* `RCLONE_REMOTE`: not set (credentials never present here — nothing is ever
  exposed in logs, the repo, or this doc).

## What exists

`scripts/backup.sh`:
1. `pg_dump -Fc` (custom-format) to `backups/voxdesk-<ts>.dump`.
2. Integrity check — `pg_restore --list` must succeed, else exit non-zero
   ("an unreadable dump is not a backup").
3. Retention — keep the last 14 local dumps.
4. Off-site — `rclone copy <out_dir> <RCLONE_REMOTE>/voxdesk-backups` when
   `RCLONE_REMOTE` is set (secrets come from the environment, never argv).

`scripts/restore.sh` (inverse; guarded, never auto-downgrades).

## Operator procedure (to be executed on real infrastructure)

1. **Configure the remote once:** `rclone config` (or mount the secret), then
   set `RCLONE_REMOTE` (e.g. `s3:voxdesk-backups`) in the environment; never
   commit it.
2. **Smoke the remote:** `rclone lsd "$RCLONE_REMOTE"` — must list without
   error, then `rclone mkdir "$RCLONE_REMOTE"/voxdesk-backups`.
3. **Run:** `scripts/backup.sh` — confirm "Backup verified:" and the rclone
   "Syncing to" line.
4. **Verify remotely:** `rclone ls "$RCLONE_REMOTE"/voxdesk-backups` shows the
   new dump; download it and run `pg_restore --list` locally to prove the
   remote copy is readable.
5. **Restore drill (non-production only):** on a scratch database run
   `scripts/restore.sh <dump>`, then `SELECT count(*)` sanity checks on key
   tables (`tenants`, `calls`, `audit_logs`). Never restore over production
   without a fresh backup and an explicit operator decision.

## Never

* Expose `RCLONE_REMOTE`, rclone tokens, or `PGPASSWORD` in logs/CI/this repo.
* Claim off-site backup works until step 2–5 above have actually run.
* Treat a local-only dump as a complete backup.
