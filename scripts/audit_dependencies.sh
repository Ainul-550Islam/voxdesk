#!/usr/bin/env bash
#
# Dependency audit with documented accepted risks (STEP 9, revised STEP 10).
#
# Hard-fails on any vulnerable package EXCEPT the three listed below, which are
# deliberately pinned and documented in docs/SECURITY.md under "Accepted
# risks". Everything else must be clean or the build fails.
#
#   nltk    pipecat-ai 0.0.94 base dependency; the installed 3.10.3 is already
#           the version OSV marks as fixed, so this is a boundary false
#           positive (no newer 3.x exists) — see docs/SECURITY.md
#   pillow  pipecat-ai 0.0.94 base pins Pillow<12; the fixes are all in 12.x,
#           and VoxDesk's audio-only pipeline never decodes images, so the
#           image-format advisories are not reachable — see docs/SECURITY.md
#   pytest  test-only dependency (tmpdir handling); the fix is the 9.0.3 major
#           line, deferred to avoid destabilising the 2254-test suite — see
#           docs/SECURITY.md
#
# STEP 10 resolved the rest of the old list: cryptography (44.0.1 -> 50.0.1),
# starlette (0.41.3 -> 1.6.0 via fastapi 0.136.1), aiohttp and pipecat-ai
# (both clean at 3.14.3 / 0.0.94), so they were removed from this list.
#
# Exit 0 when every finding is an accepted risk, exit 1 otherwise.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v pip-audit >/dev/null 2>&1; then
  echo "pip-audit is not installed; run: pip install pip-audit" >&2
  exit 1
fi

# pip-audit prints the report to stdout and exits 1 when anything is found,
# so capture stdout and ignore the exit code.
report="$(pip-audit -r requirements.txt -f json 2>/dev/null || true)"

ACCEPTED='["nltk", "pillow", "pytest"]' \
python3 -c '
import json
import os
import sys

accepted = set(json.loads(os.environ["ACCEPTED"]))
report = sys.stdin.read()
try:
    doc = json.loads(report)
except json.JSONDecodeError:
    print("audit_dependencies: pip-audit produced no parseable JSON")
    sys.exit(1)

found = {}
for dep in doc.get("dependencies", []):
    vulns = dep.get("vulns") or []
    if vulns:
        found[dep["name"]] = [v.get("id") for v in vulns]

unaccepted = {n: ids for n, ids in found.items() if n not in accepted}
accepted_hits = {n: ids for n, ids in found.items() if n in accepted}

if unaccepted:
    print("FAIL: vulnerable dependencies outside the accepted-risk list:")
    for name, ids in sorted(unaccepted.items()):
        shown = ", ".join(ids[:6]) + ("..." if len(ids) > 6 else "")
        print(f"  - {name}: {len(ids)} advisories ({shown})")
    sys.exit(1)

print("OK: no unexpected vulnerable dependencies.")
for name, ids in sorted(accepted_hits.items()):
    print(f"  accepted risk: {name} ({len(ids)} advisories — see docs/SECURITY.md)")
sys.exit(0)
' <<< "$report"
