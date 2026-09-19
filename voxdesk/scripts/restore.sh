#!/bin/sh
# VoxDesk Postgres restore.
#
# Usage:
#   scripts/restore.sh path/to/voxdesk-YYYYMMDD-HHMMSS.dump
#
# By default it restores into the database named by POSTGRES_DB. For a DRILL
# (restore into a scratch database without touching the live one), set
# RESTORE_TARGET_DB=voxdesk_restore_drill — the dump is then restored into
# that database, never into the live one.
#
# Safety guards:
#   * The dump is verified with `pg_restore --list` BEFORE anything is written.
#   * If the target database already contains tables, the script refuses to
#     proceed unless RESTORE_ALLOW_OVERWRITE=1 — a restore must not silently
#     clobber a live database during a drill.
#   * After restoring, the script verifies the schema actually landed by
#     counting the restored tables.
#
# Secrets come from the environment, never from argv.
set -eu

DUMP="${1:-}"
if [ -z "${DUMP}" ] || [ ! -f "${DUMP}" ]; then
  echo "usage: scripts/restore.sh <dump-file>" >&2
  exit 2
fi

DB_USER="${POSTGRES_USER:-voxdesk}"
DB_NAME="${RESTORE_TARGET_DB:-${POSTGRES_DB:-voxdesk}}"
DB_HOST="${PGHOST:-db}"
ALLOW_OVERWRITE="${RESTORE_ALLOW_OVERWRITE:-0}"

echo "Verifying dump integrity..."
pg_restore --list "${DUMP}" > /dev/null
echo "Dump verified: ${DUMP}"

# Refuse to clobber a database that already has data (e.g. a live prod DB)
# unless the operator has explicitly authorised an overwrite.
if [ "${ALLOW_OVERWRITE}" != "1" ]; then
  TABLE_COUNT="$(psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -tAc \
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'" 2>/dev/null || echo 0)"
  if [ "${TABLE_COUNT:-0}" != "0" ]; then
    echo "ERROR: target database '${DB_NAME}' already has ${TABLE_COUNT} tables." >&2
    echo "Refusing to restore over it. Use RESTORE_TARGET_DB=<scratch> for a drill," >&2
    echo "or RESTORE_ALLOW_OVERWRITE=1 to overwrite deliberately." >&2
    exit 3
  fi
fi

echo "Restoring ${DUMP} into ${DB_NAME}@${DB_HOST}"
pg_restore -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" --clean --if-exists "${DUMP}"

# Post-restore verification: the schema must have actually landed.
RESTORED_TABLES="$(psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "${RESTORED_TABLES:-0}" = "0" ]; then
  echo "ERROR: restore reported success but no tables are present." >&2
  exit 1
fi
echo "Restore complete and verified: ${RESTORED_TABLES} tables in ${DB_NAME}."
