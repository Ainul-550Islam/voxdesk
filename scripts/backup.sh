#!/bin/sh
# VoxDesk Postgres backup (manual run; complements the nightly backup service).
#
# Usage:
#   scripts/backup.sh [output_dir]
#
# Produces a compressed custom-format dump, VERIFIES it is readable with
# `pg_restore --list`, retains the last 14 local dumps, and optionally syncs
# to an S3-compatible remote when RCLONE_REMOTE is configured (rclone must be
# installed). Secrets come from the environment, never from argv.
#
# A backup that fails its integrity check exits non-zero — an unreadable dump
# is not a backup, and pretending otherwise is how restore drills fail at the
# worst possible moment.
set -eu

OUT_DIR="${1:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="${OUT_DIR}/voxdesk-${STAMP}.dump"

mkdir -p "${OUT_DIR}"

DB_USER="${POSTGRES_USER:-voxdesk}"
DB_NAME="${POSTGRES_DB:-voxdesk}"
DB_HOST="${PGHOST:-db}"

echo "Backing up ${DB_NAME}@${DB_HOST} -> ${FILE}"
pg_dump -h "${DB_HOST}" -U "${DB_USER}" -Fc "${DB_NAME}" > "${FILE}"

# Integrity check: the dump must exist, be non-empty, and be readable.
if [ ! -s "${FILE}" ]; then
  echo "ERROR: backup is empty or missing: ${FILE}" >&2
  exit 1
fi
pg_restore --list "${FILE}" > /dev/null
echo "Backup verified: ${FILE} ($(du -h "${FILE}" | cut -f1))"

# Keep the last 14 local dumps.
ls -1 "${OUT_DIR}"/*.dump 2>/dev/null | head -n -14 | xargs -r rm

if [ -n "${RCLONE_REMOTE:-}" ]; then
  echo "Syncing to ${RCLONE_REMOTE}"
  rclone copy "${OUT_DIR}" "${RCLONE_REMOTE}"/voxdesk-backups
fi
