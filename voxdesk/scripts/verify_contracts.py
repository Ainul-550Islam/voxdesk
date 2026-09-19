#!/usr/bin/env python3
"""Contract verification — non-destructive.

Phase 0 of the polyglot roadmap introduces a protobuf wire contract
(``contracts/proto``) that every future non-Python service (Rust control
plane, C++ media plane, Go ops tooling) must speak. This script is the gate
that keeps that contract honest. It performs **no network I/O, no database
access, and no writes** beyond a throwaway descriptor file in a temporary
directory.

Three checks run, in order:

1. **Compile** — every ``.proto`` file must compile with ``protoc`` (syntax and
   cross-file reference validation). Skips only when ``--skip-compile`` is
   passed or ``protoc`` cannot be found and the flag is given.

2. **Enum vocabulary** — the protobuf enum member names must match the Python
   enum member names in ``app/db/models.py`` exactly. That is deliberate:
   SQLAlchemy persists the enum member *name* (not the value) into PostgreSQL,
   so a drift between the wire vocabulary and the stored vocabulary is a real
   data-integrity bug, not a style issue. For ``CrmEventType`` the dotted
   published values (``call.completed``, ...) are additionally derived from the
   member names and compared against the Python values.

3. **Tenant scoping** — every operational message (everything except the four
   shared primitives ``Uuid``, ``TenantContext``, ``Idempotency``, ``ErrorInfo``)
   must carry a ``tenant_id`` field or embed a ``TenantContext``. A message
   that can cross a service boundary without a tenant binding is a
   tenant-isolation risk, so this is a hard error rather than a warning.

Exit code 0 when every check passes, 1 otherwise. ``--json`` emits a
machine-readable report (stable keys) for CI tooling.

Usage::

    python scripts/verify_contracts.py [--protoc PATH] [--skip-compile] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts" / "proto"

# The model cross-check imports app.db.models, which resolves relative to the
# repository root — not the script's own directory.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# proto enum name -> Python enum class name in app.db.models. The member-name
# cross-check is what keeps the wire vocabulary and the stored vocabulary
# identical.
MODEL_ENUMS: dict[str, str] = {
    "CallStatus": "CallStatus",
    "TransferState": "TransferState",
    "CallDirection": "CallDirection",
    "Speaker": "Speaker",
    "UsageMetric": "UsageMetric",
    "UsageEventType": "UsageEventType",
    "CrmEntityType": "CrmEntityType",
    "CrmSyncStatus": "CrmSyncStatus",
}

# proto enum name -> Python enum class name that additionally carries dotted
# published values (e.g. CALL_COMPLETED -> "call.completed"). These values are
# part of the tenant-facing webhook contract, so they are checked too.
DOTTED_VALUE_ENUMS: dict[str, str] = {
    "CrmEventType": "CrmEventType",
}

# Messages exempt from the tenant-scoping rule: they are shared primitives that
# are always embedded in a tenant-scoped message, never sent on their own.
PRIMITIVE_MESSAGES = frozenset({"Uuid", "TenantContext", "Idempotency", "ErrorInfo"})

# A protobuf enum member name looks like CALL_STATUS_RINGING: the SCREAMING_
# SNAKE prefix of the enum name followed by the member name. Strip the prefix
# to recover the Python member name ("RINGING").
_SCREAM = re.compile(r"(?<!^)(?=[A-Z])")

_ENUM_RE = re.compile(r"\benum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_MESSAGE_RE = re.compile(r"\bmessage\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_MEMBER_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*-?\d+\s*;", re.M)
_FIELD_RE = re.compile(
    r"^\s*(?:repeated\s+|optional\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s+([a-z_][a-z0-9_]*)\s*=\s*\d+\s*;",
    re.M,
)


def screaming(name: str) -> str:
    """``CallStatus`` -> ``CALL_STATUS``."""
    return _SCREAM.sub("_", name).upper()


def strip_comments(text: str) -> str:
    """Remove ``//``, ``#`` and ``/* ... */`` comments from a .proto body."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.split(r"//|#", line)[0] for line in text.splitlines())


def _blocks(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """Return ``[(name, body), ...]`` for the blocks matched by ``pattern``."""
    blocks: list[tuple[str, str]] = []
    index = 0
    while True:
        match = pattern.search(text, index)
        if not match:
            break
        name = match.group(1)
        start = match.end()
        depth = 1
        cursor = start
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        blocks.append((name, text[start : cursor - 1]))
        index = cursor
    return blocks


def parse_proto(path: Path) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]]]:
    """Parse one ``.proto`` file into ``(enums, messages)``.

    ``enums`` maps enum name -> list of member names; ``messages`` maps message
    name -> list of ``(field_type, field_name)`` tuples. ``map<...>`` fields are
    intentionally not recorded: they can never carry the tenant binding, so the
    tenant-scoping check has nothing to learn from them.
    """
    text = strip_comments(path.read_text(encoding="utf-8"))
    enums: dict[str, list[str]] = {}
    messages: dict[str, list[tuple[str, str]]] = {}
    for name, body in _blocks(text, _ENUM_RE):
        enums[name] = _MEMBER_RE.findall(body)
    for name, body in _blocks(text, _MESSAGE_RE):
        messages[name] = _FIELD_RE.findall(body)
    return enums, messages


def collect_proto_files() -> list[Path]:
    """Every ``.proto`` under ``contracts/proto``, in stable order."""
    return sorted(CONTRACTS_DIR.rglob("*.proto"))


def find_protoc(explicit: str | None) -> str | None:
    """Locate a usable protoc binary; ``None`` when none can be found."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("VOXDESK_PROTOC")
    if env:
        candidates.append(env)
    on_path = shutil.which("protoc")
    if on_path:
        candidates.append(on_path)
    candidates += ["/usr/bin/protoc", "/usr/local/bin/protoc"]
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() or shutil.which(candidate):
            return str(path if path.is_file() else shutil.which(candidate))
    return None


def compile_protos(protoc: str, files: list[Path]) -> tuple[bool, str]:
    """Compile all contract files to a throwaway descriptor set.

    Returns ``(ok, stderr)``. The descriptor set lives in a temporary
    directory that is removed before returning, so this never writes into the
    working tree.
    """
    include_dir = Path(protoc).resolve().parent.parent / "include"
    args = [protoc, f"--proto_path={CONTRACTS_DIR}"]
    if include_dir.is_dir():
        args.append(f"--proto_path={include_dir}")
    with tempfile.TemporaryDirectory() as tmp:
        args += [f"--descriptor_set_out={Path(tmp) / 'contracts.pb'}", "--include_imports"]
        args += [str(f) for f in files]
        proc = subprocess.run(args, capture_output=True, text=True)
        return proc.returncode == 0, proc.stderr


def check_enums_against_models(enums: dict[str, list[str]], models) -> list[dict[str, str]]:
    """Cross-check the contract enum vocabulary against app.db.models."""
    errors: list[dict[str, str]] = []
    for proto_name, py_name in {**MODEL_ENUMS, **DOTTED_VALUE_ENUMS}.items():
        members = enums.get(proto_name)
        if members is None:
            errors.append({"kind": "missing-enum", "package": proto_name,
                           "detail": f"no enum named '{proto_name}' in the contracts"})
            continue
        py_enum = getattr(models, py_name)
        prefix = screaming(proto_name)
        unspecified = f"{prefix}_UNSPECIFIED"
        for member in members:
            if not member.startswith(f"{prefix}_"):
                errors.append({"kind": "bad-member-name", "package": proto_name,
                               "detail": f"member '{member}' does not use the "
                                         f"'{prefix}_' prefix"})
        actual = {member[len(prefix) + 1 :] for member in members if member != unspecified}
        expected = set(py_enum.__members__.keys())
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            if missing:
                errors.append({"kind": "enum-member-missing", "package": proto_name,
                               "detail": f"contract is missing {missing}"})
            if extra:
                errors.append({"kind": "enum-member-extra", "package": proto_name,
                               "detail": f"contract has members unknown to the "
                                         f"model: {extra}"})
        if proto_name in DOTTED_VALUE_ENUMS:
            for member in members:
                if member == unspecified:
                    continue
                short = member[len(prefix) + 1 :]
                if short not in py_enum.__members__:
                    continue  # already reported as extra/missing above
                parts = short.split("_")
                derived = parts[0].lower() + "." + "_".join(parts[1:]).lower()
                if derived != py_enum[short].value:
                    errors.append({"kind": "enum-value-mismatch", "package": proto_name,
                                   "detail": f"'{short}' derives to '{derived}' but the "
                                             f"model value is '{py_enum[short].value}'"})
    return errors


def check_tenant_scoping(messages: dict[str, list[tuple[str, str]]]) -> list[dict[str, str]]:
    """Every non-primitive message must carry a tenant binding."""
    errors: list[dict[str, str]] = []
    for name, fields in messages.items():
        if name in PRIMITIVE_MESSAGES:
            continue
        scoped = any(
            field_name == "tenant_id" or field_type.rsplit(".", 1)[-1] == "TenantContext"
            for field_type, field_name in fields
        )
        if not scoped:
            errors.append({"kind": "tenant-unscoped", "package": name,
                           "detail": "message crosses a service boundary but carries no "
                                     "tenant_id field and no TenantContext field"})
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protoc", help="path to the protoc binary")
    parser.add_argument("--skip-compile", action="store_true",
                        help="skip the protoc compile step (textual checks only)")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    files = collect_proto_files()
    if not files:
        message = f"no .proto files found under {CONTRACTS_DIR}"
        if args.json:
            json.dump({"ok": False, "errors": [{"kind": "no-files",
                                                "package": "contracts",
                                                "detail": message}]},
                      sys.stdout, indent=2)
        else:
            print(f"FAIL  {message}")
        return 1

    enums: dict[str, list[str]] = {}
    messages: dict[str, list[tuple[str, str]]] = {}
    parse_errors: list[dict[str, str]] = []
    for path in files:
        try:
            file_enums, file_messages = parse_proto(path)
        except Exception as exc:  # report every cause; never crash the report
            parse_errors.append({"kind": "parse", "package": path.name,
                                 "detail": str(exc)})
            continue
        for name, members in file_enums.items():
            enums.setdefault(name, []).extend(members)
        for name, fields in file_messages.items():
            messages[name] = fields

    compile_ok = True
    compile_stderr = ""
    protoc = find_protoc(args.protoc)
    if not args.skip_compile:
        if protoc is None:
            compile_ok = False
            compile_stderr = ("protoc not found; install protobuf-compiler or pass "
                              "--protoc / set VOXDESK_PROTOC")
        else:
            compile_ok, compile_stderr = compile_protos(protoc, files)

    try:
        from app.db import models
    except Exception as exc:  # pragma: no cover - environment-specific
        models = None
        model_errors = [{"kind": "model-import", "package": "app.db.models",
                         "detail": f"cannot import app.db.models: {exc}"}]
    else:
        model_errors = check_enums_against_models(enums, models)

    scoping_errors = check_tenant_scoping(messages)

    errors: list[dict[str, str]] = []
    if not compile_ok:
        errors.append({"kind": "compile", "package": "contracts",
                       "detail": compile_stderr.strip() or "protoc failed"})
    errors += parse_errors + model_errors + scoping_errors
    ok = not errors

    if args.json:
        json.dump({
            "ok": ok,
            "contracts_dir": str(CONTRACTS_DIR),
            "files": [str(f.relative_to(REPO_ROOT)) for f in files],
            "protoc": protoc,
            "compile_ok": compile_ok,
            "errors": errors,
        }, sys.stdout, indent=2)
    else:
        print(f"contracts: {len(files)} .proto files")
        for problem in errors:
            print(f"FAIL  {problem['package']:24s} [{problem['kind']}] {problem['detail']}")
        if ok:
            print("OK: contracts compile; enum vocabulary matches app.db.models; "
                  "every operational message is tenant-scoped.")
        else:
            print(f"contract verification FAILED: {len(errors)} problem(s).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
