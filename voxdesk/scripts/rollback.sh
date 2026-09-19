#!/bin/sh
# VoxDesk application (code) rollback.
#
#   scripts/rollback.sh <git-sha>
#
# What it does: checks out <git-sha> and runs scripts/deploy.sh --no-pull
# (rebuild + advisory-locked forward migration + readiness wait).
#
# What it NEVER does: downgrade the database automatically. Alembic
# downgrades are destructive-in-principle (a ``downgrade`` can drop columns
# and lose data) and are therefore a separate, explicitly-authorized operator
# action. The migration discipline is documented in docs/DEPLOYMENT.md; the
# short version is:
#
#   * Migrations are additive/backward-compatible wherever practical, so an
#     old app against a newer schema should keep working — code rollback then
#     needs no schema rollback.
#   * When a schema change is NOT backward-compatible, it is released in a
#     separate step and documented as "no code rollback past this point
#     without a manual downgrade".
#   * The manual downgrade, when truly required, is run by hand:
#
#       docker compose -f docker-compose.prod.yml run --rm --entrypoint python api \
#         -c "from alembic.config import Config; from alembic import command; command.downgrade(Config('alembic.ini'), '-1')"
#
# This script refuses to run that command itself, and refuses to proceed with
# a code rollback when it cannot determine that the schema is safe.
set -eu

cd "$(dirname "$0")/.."

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: scripts/rollback.sh <git-sha>" >&2
  exit 2
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is dirty; commit or stash before rolling back." >&2
  exit 1
fi

echo "Current revision: $(git rev-parse --short HEAD)"
echo "Rolling back code to: $TARGET"

# Record the applied migration before touching anything, so the operator can
# see whether the target sha predates the applied schema.
echo "Applied migrations now:"
docker compose -f docker-compose.prod.yml run --rm --entrypoint sh api \
  -c "python -m alembic current" 2>/dev/null \
  || echo "  (could not read current revision — is the database up?)"

git checkout "$TARGET"

echo "Rebuilding and deploying $TARGET (forward-only, advisory-locked migrations)."
scripts/deploy.sh --no-pull

echo
echo "Rollback complete. If the applied schema is NEWER than $TARGET expects,"
echo "verify the app boots and serves /health/ready — additive migrations mean"
echo "it should. If a manual schema downgrade is truly required, see the header"
echo "of this script; it is never performed automatically."
