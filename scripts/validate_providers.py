"""Real-provider validation runner. See docs/INTEGRATION-VALIDATION.md.

Run it from the repository root:

    VOXDESK_REAL_INTEGRATION=1 python -m scripts.validate_providers
    # or, equivalently:
    VOXDESK_REAL_INTEGRATION=1 python scripts/validate_providers.py

Without the opt-in flag no network call is made; every provider reports
SKIPPED. The same command is available as
``python -m app.integrations.validation.cli``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow both `python -m scripts.validate_providers` and a direct
# `python scripts/validate_providers.py` invocation: put the repository root
# on the path so `app.*` imports resolve either way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.validation.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
