#!/bin/sh
# VoxDesk safe deploy for the single-VM compose topology.
#
#   scripts/deploy.sh [--no-build] [--no-backup] [--no-pull]
#
# Sequence (see docs/DEPLOYMENT.md for the honest zero-downtime statement):
#   1. pull latest code (fast-forward only)
#   2. build images
#   3. pre-deploy database backup + integrity check (fail-closed; the backup
#      is the rollback point; opt out only with --no-backup)
#   4. apply migrations under an advisory lock (scripts/migrate.py)
#   5. recreate containers onto the new image
#   6. wait for readiness (up to 120s)
#   7. verify liveness and report
#
# On any failure the script prints the rollback command. It never downgrades
# the database automatically — a forward migration is only ever reversed by an
# explicit, operator-run `scripts/rollback.sh` with its own guard.
set -eu

cd "$(dirname "$0")/.."

NO_BUILD=0
NO_BACKUP=0
NO_PULL=0
for arg in "$@"; do
  case "$arg" in
    --no-build) NO_BUILD=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    --no-pull) NO_PULL=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

rollback_hint() {
  echo "Rollback: scripts/rollback.sh <previous-good-git-sha>" >&2
}

# 1. Pull latest code (skipped by rollback.sh, which must stay on the sha it
#    checked out).
if [ "$NO_PULL" != "1" ]; then
  git pull --ff-only
fi

# 2. Build images.
if [ "$NO_BUILD" != "1" ]; then
  docker compose -f docker-compose.prod.yml build
fi

# 3. Pre-deploy backup. A deploy that cannot back up must not migrate.
if [ "$NO_BACKUP" != "1" ]; then
  echo "Pre-deploy backup..."
  docker compose -f docker-compose.prod.yml run --rm --entrypoint sh backup -c \
    'pg_dump -h db -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > /backups/voxdesk-predeploy.dump'
  # Integrity check: the dump must exist, be non-empty, and be readable by
  # pg_restore. A backup we cannot read is not a rollback point.
  if [ ! -s backups/voxdesk-predeploy.dump ]; then
    echo "ERROR: pre-deploy backup is empty or missing." >&2
    rollback_hint
    exit 1
  fi
  docker compose -f docker-compose.prod.yml run --rm --entrypoint sh backup -c \
    'pg_restore --list /backups/voxdesk-predeploy.dump > /dev/null' || {
    echo "ERROR: pre-deploy backup failed integrity check (pg_restore --list)." >&2
    rollback_hint
    exit 1
  }
  echo "Pre-deploy backup verified: backups/voxdesk-predeploy.dump"
fi

# 4. Migrate under an advisory lock before any new code serves traffic.
docker compose -f docker-compose.prod.yml up -d db redis
docker compose -f docker-compose.prod.yml run --rm --entrypoint python api \
  scripts/migrate.py

# 5. Recreate everything onto the new image.
docker compose -f docker-compose.prod.yml up -d

# 6. Wait for readiness (up to 120s). Readiness checks DB + configured Redis
#    + (production) provider config, so it is the correct "can serve" signal.
echo "Waiting for readiness..."
READY=0
i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS http://localhost:8000/health/ready >/dev/null 2>&1; then
    READY=1
    break
  fi
  i=$((i + 1))
  sleep 2
done
if [ "$READY" != "1" ]; then
  echo "ERROR: /health/ready did not become OK within 120s." >&2
  docker compose -f docker-compose.prod.yml logs --tail 100 api >&2 || true
  rollback_hint
  exit 1
fi

# 7. Verify liveness and report.
curl -fsS http://localhost:8000/health && echo
echo "Deploy complete."
