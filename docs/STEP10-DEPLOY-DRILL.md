# VoxDesk — Deployment drill record (Step 10, item O)

## Status

**Static validation only — no deployment was executed.** This sandbox has no
Docker (`command -v docker` → absent), no compose runtime and no database, so
the drill below is a **record template** plus the exact blockers. Claiming a
deploy/rollback/restore PASS without running it is explicitly forbidden by the
project rules.

## What exists (reviewed, not executed)

`scripts/deploy.sh` sequence:
1. `git pull --ff-only` (fast-forward only — a divergent tree is an operator
   decision, not an auto-merge).
2. `docker compose -f docker-compose.prod.yml build`.
3. **Pre-deploy backup + integrity check** (`scripts/backup.sh`); a deploy
   that cannot back up must not migrate (`--no-backup` is the only opt-out).
4. Migrations under an advisory lock (`scripts/migrate.py`).
5. Recreate containers onto the new image.
6. Wait for readiness (≤ 120 s).
7. Verify liveness and report; on failure print the rollback command.

`scripts/rollback.sh` restores to a previous good git sha;
`scripts/restore.sh` restores a dump. **The database is never automatically
downgraded** — a forward migration is only reversed by an explicit,
operator-run rollback with its own guard.

## Drill record (fill in when executed on real infrastructure)

| Field | Value |
|---|---|
| Date / operator | |
| Environment | staging (never production first) |
| Commit sha deployed | |
| Backup taken & verified | ☐ `pg_restore --list` OK |
| Migrations applied | ☐ under advisory lock, forward-only |
| Readiness passed | ☐ `/health/ready` green |
| Smoke passed | ☐ dashboard, `/metrics`, scheduler, WS |
| Rollback exercised | ☐ to previous sha, restore verified |
| Notes | |

## Blocker (this step)

No container runtime or database in the execution environment → the drill is
**NOT_RUN**. The scripts are the deliverable; the execution is an operator
task on staging infrastructure, recorded in the table above when performed.
