#!/usr/bin/env python3
"""VoxDesk production launch gate (Step 11).

Thin entry point: adds the repository root to ``sys.path`` and delegates to
``app.release.cli.main``. Run from anywhere in the tree:

    python scripts/release_gate.py [--freeze] [--json] [--provider-results PATH]

The gate performs no network calls and no phone calls; real-provider results
are ingested only from an explicitly produced file and only under the opt-in
switch ``VOXDESK_REAL_INTEGRATION=1``.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.release.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
