"""Contract tests — no protoc, no network, no database.

These tests assert the two properties the Phase 0 wire contract must hold
regardless of which toolchains are installed:

1. The protobuf enum vocabulary matches app.db.models exactly (member names),
   including the dotted published values for CrmEventType.
2. Every operational message is tenant-scoped.

The protoc *compile* step lives in scripts/verify_contracts.py (and the CI
polyglot workflow) because it needs the protoc binary; these textual checks
are the part that must pass inside the ordinary backend suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import verify_contracts as vc  # noqa: E402


def _parse_all() -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]]]:
    enums: dict[str, list[str]] = {}
    messages: dict[str, list[tuple[str, str]]] = {}
    for path in vc.collect_proto_files():
        file_enums, file_messages = vc.parse_proto(path)
        for name, members in file_enums.items():
            enums.setdefault(name, []).extend(members)
        for name, fields in file_messages.items():
            messages[name] = fields
    return enums, messages


def test_contracts_exist() -> None:
    files = vc.collect_proto_files()
    assert files, "no .proto files under contracts/proto"
    names = {f.name for f in files}
    assert {"common.proto", "session.proto", "media.proto", "usage.proto",
            "webhook.proto"} <= names


def test_enum_vocabulary_matches_models() -> None:
    from app.db import models

    enums, _ = _parse_all()
    errors = vc.check_enums_against_models(enums, models)
    assert errors == [], errors


def test_every_operational_message_is_tenant_scoped() -> None:
    _, messages = _parse_all()
    errors = vc.check_tenant_scoping(messages)
    assert errors == [], errors


def test_shared_primitives_are_exactly_the_allowlist() -> None:
    # The tenant-scoping allowlist is a security decision: if a new shared
    # primitive is introduced it must be added here deliberately, and this
    # test makes forgetting it loud.
    assert vc.PRIMITIVE_MESSAGES == frozenset(
        {"Uuid", "TenantContext", "Idempotency", "ErrorInfo"}
    )
