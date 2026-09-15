"""Environment validation command (Step 4).

    VOXDESK_REAL_INTEGRATION=1 python -m app.integrations.validation.cli
    VOXDESK_REAL_INTEGRATION=1 python -m app.integrations.validation.cli --provider Twilio
    VOXDESK_REAL_INTEGRATION=1 python -m app.integrations.validation.cli --json

Without the opt-in flag every check reports SKIPPED and no network call is
made. There is deliberately no ``--force`` switch: the flag is the switch.
All output is passed through a ``SecretMasker`` built from every credential
the checks read, so a token can never appear in the table even if a check
bug interpolated one.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from app.core.config import settings
from app.integrations.validation.registry import (
    FORBIDDEN_OPERATIONS,
    MANUAL_E2E_NOTE,
    run_all,
    run_one,
)
from app.integrations.validation.report import (
    format_table,
    summary_counts,
    to_json,
)
from app.integrations.validation.status import (
    CheckStatus,
    SecretMasker,
    real_integration_enabled,
)


def _collect_secrets() -> list[str]:
    """Every credential the checks might have read, for output scrubbing."""
    env = os.environ
    candidates = [
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.deepgram_api_key,
        settings.openai_api_key,
        settings.anthropic_api_key,
        settings.google_api_key,
        settings.elevenlabs_api_key,
        settings.stripe_secret_key,
        settings.stripe_webhook_secret,
        env.get("VOXDESK_REAL_GOOGLE_CALENDAR_REFRESH_TOKEN", ""),
        env.get("VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_ID", ""),
        env.get("VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_SECRET", ""),
        env.get("VOXDESK_REAL_GOOGLE_CALENDAR_ACCESS_TOKEN", ""),
        env.get("VOXDESK_REAL_MICROSOFT_REFRESH_TOKEN", ""),
        env.get("VOXDESK_REAL_MICROSOFT_CLIENT_ID", ""),
        env.get("VOXDESK_REAL_MICROSOFT_CLIENT_SECRET", ""),
        env.get("VOXDESK_REAL_MICROSOFT_ACCESS_TOKEN", ""),
        env.get("VOXDESK_REAL_CALCOM_API_KEY", ""),
        env.get("VOXDESK_REAL_HUBSPOT_TOKEN", ""),
        env.get("VOXDESK_REAL_GHL_ACCESS_TOKEN", ""),
        env.get("VOXDESK_REAL_JOBBER_ACCESS_TOKEN", ""),
    ]
    return [c for c in candidates if c]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate-providers",
        description="Validate the real external providers VoxDesk integrates with.",
    )
    parser.add_argument(
        "--provider", metavar="NAME", default=None,
        help="run a single provider by name (e.g. 'Twilio', 'Stripe')",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON instead of the table",
    )
    return parser


async def _run(provider: str | None):
    if provider:
        return [await run_one(provider)]
    return await run_all()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    masker = SecretMasker(_collect_secrets())

    if not real_integration_enabled():
        # stderr, never stdout: a --json run must stay valid JSON.
        print(
            "Real-provider validation is DISABLED. "
            "Set VOXDESK_REAL_INTEGRATION=1 to enable it.\n",
            file=sys.stderr,
        )

    outcomes = asyncio.run(_run(args.provider))

    if args.json:
        print(to_json(outcomes))
    else:
        print(format_table(outcomes, masker=masker))
        counts = summary_counts(outcomes)
        total = sum(counts.values())
        print()
        print(
            "  ".join(f"{k}={v}" for k, v in counts.items())
            + f"  (total {total})"
        )
        print()
        print("Safety: every runnable check above is read-only. The following")
        print("operations are never automated:")
        for who, what in FORBIDDEN_OPERATIONS:
            print(f"  - {who}: {what}")
        print()
        print(f"Note: {MANUAL_E2E_NOTE}")

    failed = summary_counts(outcomes).get(CheckStatus.FAIL.value, 0)
    blocked = summary_counts(outcomes).get(CheckStatus.BLOCKED.value, 0)
    return 1 if (failed or blocked) else 0


if __name__ == "__main__":
    sys.exit(main())
