#!/usr/bin/env python3
"""Dependency verification — non-destructive.

Confirms that a development, CI or production Python environment matches the
canonical ``requirements.txt`` without installing, uninstalling, downgrading or
otherwise modifying anything. Importing a module only executes its module-level
code; every dependency VoxDesk imports is import-only, so this script performs
no network I/O, never touches the database, never writes files and never calls
an external provider.

Two checks run, in order:

1. **Declared-package check** — every distribution pinned in
   ``requirements.txt`` must be installed, must match its pinned version
   exactly (via :mod:`importlib.metadata`), and must be importable under the
   module name the project actually uses.

2. **Source-import check** — every third-party module imported by ``app/``,
   ``scripts/``, ``tests/`` and the ``alembic/`` migrations must be importable.
   A module that imports cleanly but is not declared in ``requirements.txt``
   is reported as a *warning* (it may be a legitimate transitive dependency);
   a module that cannot be imported at all is a hard *error*.

Exit code 0 when every check passes, 1 otherwise. ``--json`` emits a
machine-readable report (stable keys) for CI tooling.

Usage::

    python scripts/verify_dependencies.py

``loadtest/`` is intentionally excluded from the source-import scan: its only
third-party import is ``locust`` (a load-testing tool installed on demand via
``pip install locust``, see docs/LOAD-TESTING.md), and its
``from safety import validate_target`` fallback refers to the project's own
``loadtest/safety.py`` when Locust runs the file as a script, not to a PyPI
package.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Distribution name -> importable module name, for packages whose import name
# does not follow the "hyphens become underscores" rule, or whose top-level
# module differs from their PyPI name. Keys are lower-cased because PyPI
# distribution names are case-insensitive and requirements.txt may spell a
# name in any case.
DIST_IMPORT_OVERRIDES: dict[str, str] = {
    "pyjwt": "jwt",
    "pyyaml": "yaml",
    "python-docx": "docx",
    "python-multipart": "multipart",
    "python-dotenv": "dotenv",
    "deepgram-sdk": "deepgram",
    "google-api-python-client": "googleapiclient",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "pipecat-ai": "pipecat",
}

# Top-level modules imported by the project that are provided transitively by a
# declared distribution and are therefore not listed in requirements.txt on
# purpose. Importing them is fine; failing to import them is still an error.
TRANSITIVE_ALLOWLIST: frozenset[str] = frozenset({
    "pydantic_core",   # pinned transitive dependency of pydantic
    "google",          # google-auth namespace (dep of google-api-python-client)
})

# The project's own top-level packages — never treated as third-party.
LOCAL_PACKAGES: frozenset[str] = frozenset({"app", "tests", "scripts", "loadtest"})

# requirements.txt lines are `name==version` or `name[extra1,extra2]==version`;
# inline comments may follow. Non-pinned lines are skipped (none exist today).
_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.]+)")


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    """Return ``[(distribution_name, pinned_version), ...]`` in file order."""
    pins: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN_RE.match(line)
        if match:
            pins.append((match.group(1), match.group(2)))
    return pins


def import_name_for(distribution: str) -> str:
    """The module name the project uses for a declared distribution."""
    return DIST_IMPORT_OVERRIDES.get(
        distribution.lower(), distribution.replace("-", "_").lower()
    )


def check_declared(pins: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Verify each pinned distribution is installed, version-matched, importable."""
    problems: list[dict[str, str]] = []
    for dist, pinned in pins:
        try:
            installed = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            problems.append({"package": dist, "kind": "missing",
                             "detail": f"not installed (pinned {pinned})"})
            continue
        if installed != pinned:
            problems.append({"package": dist, "kind": "version",
                             "detail": f"pinned {pinned} but installed {installed}"})
        module = import_name_for(dist)
        try:
            importlib.import_module(module)
        except Exception as exc:  # report every cause; never crash the report
            problems.append({"package": dist, "kind": "import",
                             "detail": f"cannot import '{module}': {exc}"})
    return problems


def _stdlib_names() -> frozenset[str]:
    names = set(sys.stdlib_module_names)
    names.update({"__future__", "antigravity", "this"})
    return frozenset(names)


def scan_source() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Collect third-party imports from app/, scripts/, tests/, alembic/ and
    verify importability.

    ``loadtest/`` is not scanned: its only third-party import (``locust``) is a
    documented-optional load-testing tool, and its ``safety`` import is a local
    fallback module (see the module docstring).

    Returns ``(errors, warnings)``. ``errors`` are modules that cannot be
    imported at all; ``warnings`` are importable modules not declared in
    ``requirements.txt`` (and not on the transitive allowlist).
    """
    stdlib = _stdlib_names()
    importers: dict[str, set[str]] = {}
    for root in ("app", "scripts", "tests", "alembic"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for full in names:
                    top = full.split(".", 1)[0]
                    if top in stdlib or top in LOCAL_PACKAGES:
                        continue
                    importers.setdefault(top, set()).add(str(path.relative_to(REPO_ROOT)))

    declared = {import_name_for(d) for d, _ in parse_requirements(REQUIREMENTS)}
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for module in sorted(importers):
        files = sorted(importers[module])
        try:
            importlib.import_module(module)
        except Exception as exc:  # report every cause; never crash the report
            errors.append({"package": module, "kind": "missing",
                           "detail": f"imported by {', '.join(files[:3])}: {exc}"})
            continue
        if module not in declared and module not in TRANSITIVE_ALLOWLIST:
            warnings.append({"package": module, "kind": "undeclared",
                             "detail": f"imported by {', '.join(files[:3])}"})
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    if not REQUIREMENTS.exists():
        message = f"{REQUIREMENTS} not found"
        if args.json:
            json.dump({"ok": False, "errors": [{"package": "requirements.txt",
                                                "kind": "missing", "detail": message}]},
                      sys.stdout, indent=2)
        else:
            print(f"FAIL  {message}")
        return 1

    pins = parse_requirements(REQUIREMENTS)
    declared_problems = check_declared(pins)
    scan_errors, scan_warnings = scan_source()
    errors = declared_problems + scan_errors
    ok = not errors

    if args.json:
        json.dump({
            "ok": ok,
            "requirements_file": str(REQUIREMENTS),
            "declared": len(pins),
            "errors": errors,
            "warnings": scan_warnings,
        }, sys.stdout, indent=2)
    else:
        print(f"requirements.txt: {len(pins)} pinned distributions")
        for warning in scan_warnings:
            print(f"WARN  {warning['package']:24s} {warning['detail']}")
        for problem in errors:
            print(f"FAIL  {problem['package']:24s} [{problem['kind']}] {problem['detail']}")
        if ok:
            print("OK: all declared packages installed, importable and version-matched; "
                  "no missing source imports.")
        else:
            print(f"dependency verification FAILED: {len(errors)} problem(s) "
                  f"({len(declared_problems)} declared-package, "
                  f"{len(scan_errors)} source-import).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
