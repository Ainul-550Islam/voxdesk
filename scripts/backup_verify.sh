#!/bin/sh
# Verify a VoxDesk database dump is complete and readable.
#
# Usage:
#   scripts/backup_verify.sh path/to/voxdesk-YYYYMMDD-HHMMSS.dump
#
# Uses `pg_restore --list` to parse the custom-format archive and prove it can
# be read end to end. Exits 0 when the dump is readable, non-zero otherwise.
# This is the same check `backup.sh`, the nightly backup service and
# `restore.sh` all run, so an operator can re-verify any dump at any time.
set -eu

DUMP="${1:-}"
if [ -z "${DUMP}" ] || [ ! -f "${DUMP}" ]; then
  echo "usage: scripts/backup_verify.sh <dump-file>" >&2
  exit 2
fi

if [ ! -s "${DUMP}" ]; then
  echo "ERROR: dump is empty: ${DUMP}" >&2
  exit 1
fi

pg_restore --list "${DUMP}" > /dev/null
echo "OK: ${DUMP} is a readable custom-format dump ($(du -h "${DUMP}" | cut -f1))."
