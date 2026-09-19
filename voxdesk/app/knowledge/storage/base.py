"""
Object storage interface.

Business logic never learns whether bytes are on a local disk or in a bucket.
It holds a `key` -- an opaque string -- and asks the backend to resolve it.

Keys are always of the form `tenant/<tenant_id>/<document_id>/<filename>`, so
a listing or a prefix delete is naturally tenant-scoped, and a key from one
tenant cannot address another tenant's object without the tenant id being
wrong in the database first.
"""
from __future__ import annotations

import re
import uuid
from typing import Protocol

#: Anything that is not obviously safe inside a storage key.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
MAX_FILENAME_LENGTH = 120


class StorageError(RuntimeError):
    """Raised when the backend cannot complete an operation."""


def safe_filename(name: str | None) -> str:
    """
    Reduce a user-supplied filename to something safe to put in a key.

    Path separators and traversal sequences are stripped rather than escaped:
    an upload called `../../etc/passwd` becomes `etc_passwd`, which is inert.
    """
    candidate = (name or "").strip().replace("\\", "/").split("/")[-1]
    candidate = _UNSAFE.sub("_", candidate).strip("._-")
    if not candidate:
        candidate = "document"
    return candidate[:MAX_FILENAME_LENGTH]


def build_key(tenant_id: uuid.UUID, document_id: uuid.UUID, filename: str | None) -> str:
    """The canonical storage key for a document's original bytes."""
    return f"tenant/{tenant_id}/{document_id}/{safe_filename(filename)}"


class Storage(Protocol):
    """Minimal surface. Anything more would be speculation."""

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        """Store bytes and return the key."""
        ...

    async def get(self, key: str) -> bytes:
        ...

    async def delete(self, key: str) -> None:
        """Must not raise when the object is already gone."""
        ...

    async def exists(self, key: str) -> bool:
        ...