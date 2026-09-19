"""Deterministic fingerprints for launch-control (Step 11).

The release gate must detect drift in the artifact, dependencies, migrations
and configuration without ever depending on a mutable deployment label or a
live database. These helpers compute stable, offline fingerprints:

* ``dependency_fingerprint`` — SHA-256 over the sorted ``name==version`` pins
  in ``requirements.txt`` (comments and non-pinned lines ignored).
* ``migration_heads`` — the set of Alembic revisions that nothing revises,
  i.e. the true chain heads, parsed from ``alembic/versions/*.py`` without
  touching a database.
* ``configuration_fingerprint`` — SHA-256 over the sorted (field-name,
  default-value) pairs of the settings model, **excluding** secret-bearing
  field names, so the fingerprint changes when the config schema changes but
  never leaks a key, token, password, credential, DSN or embedded-credential
  URL.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

try:  # pragma: no cover - pydantic_core is a pinned transitive dependency
    from pydantic_core import PydanticUndefined as _UNDEFINED
except ImportError:  # pragma: no cover
    _UNDEFINED = object()

#: Field names that look secret. Matching is deliberately broad: a fingerprint
#: that omits one non-secret field is harmless; a fingerprint that includes a
#: secret is not.
_SECRET_NAME = re.compile(
    r"(secret|token|password|key|credential|dsn|salt)", re.IGNORECASE
)
#: URL-style settings that embed credentials in their value even though the
#: name does not match the regex.
_SECRET_EXACT = frozenset({"database_url", "redis_url"})

_REVISION = re.compile(r"""^revision\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_DOWN_REVISION = re.compile(
    r"""^down_revision\s*=\s*["']([^"']+)["']""", re.MULTILINE
)


def sha256_hex(text: str) -> str:
    """SHA-256 of ``text``, lowercase hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_git_dir(repo_root: Path) -> Path | None:
    """Locate the git metadata directory (handles linked worktrees)."""
    git_dir = repo_root / ".git"
    if git_dir.is_dir():
        return git_dir
    if git_dir.is_file():
        content = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        if content.startswith("gitdir:"):
            return Path(content.split(":", 1)[1].strip())
    return None


def git_commit(repo_root: str | Path) -> str:
    """The repository HEAD commit, read directly from the local git tree.

    Resolves ``.git/HEAD`` (symbolic ref via ``refs/...`` or ``packed-refs``,
    or a detached raw hash) without shelling out, so the fingerprint is
    offline and free of process-execution concerns. Returns the empty string
    when the git metadata cannot be read, which the gate treats as "cannot
    verify" (never as a match).
    """
    root = Path(repo_root)
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return ""
    head_file = git_dir / "HEAD"
    try:
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        ref_file = git_dir / ref
        try:
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        packed = git_dir / "packed-refs"
        try:
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("#") or not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == ref:
                        return parts[0]
        except OSError:
            pass
        return ""
    return head


def secret_name_excluded(name: str) -> bool:
    """True when a settings field name must be kept out of a fingerprint."""
    return name.lower() in _SECRET_EXACT or bool(_SECRET_NAME.search(name))


def dependency_fingerprint(requirements_text: str) -> str:
    """SHA-256 over the sorted ``name==version`` pins of ``requirements_text``.

    Comments, blank lines and non-pinned lines (``-e``, ``-r``, extras-only)
    are ignored, so the fingerprint tracks exactly the pinned pins.
    """
    pins: list[str] = []
    for raw in requirements_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "==" in line:
            pins.append(line)
    return sha256_hex("\n".join(sorted(pins)))


def migration_heads(alembic_versions_dir: str | Path) -> tuple[str, ...]:
    """The chain heads: every revision id that no other migration revises.

    Reads ``alembic/versions/*.py`` and parses the ``revision`` and
    ``down_revision`` assignments. Deterministic and offline; an empty or
    unreadable directory yields an empty tuple (which the caller may treat as
    "cannot verify").
    """
    root = Path(alembic_versions_dir)
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _REVISION.search(text)
        if match:
            revisions.add(match.group(1))
        match = _DOWN_REVISION.search(text)
        if match:
            down_revisions.add(match.group(1))
    heads = {rev for rev in revisions if rev not in down_revisions}
    return tuple(sorted(heads))


def configuration_fingerprint(model) -> str:
    """SHA-256 over the settings schema (names + defaults), secrets excluded.

    ``model`` is a pydantic (BaseSettings) class; ``model_fields`` is read off
    the class, so nothing environment-specific is touched. Fields without a
    default (required) are skipped, and any field whose name is secret-like is
    skipped, so the fingerprint never embeds a credential.
    """
    fields: dict[str, str] = {}
    for name in sorted(model.model_fields):
        if secret_name_excluded(name):
            continue
        info = model.model_fields[name]
        if getattr(info, "is_required", lambda: False)():
            continue
        default = info.default
        if default is _UNDEFINED:
            continue
        fields[name] = repr(default)
    return sha256_hex(str(fields))
