"""
Local filesystem storage, for development and single-node deployments.

The only genuinely interesting part is `_resolve`, which refuses to leave the
root directory. A storage key is derived from a user-supplied filename, so
`..` handling is a security control, not tidiness.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from app.knowledge.storage.base import StorageError

log = structlog.get_logger()


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/"):
            raise StorageError("invalid storage key")

        target = (self.root / key).resolve()
        # `resolve()` collapses `..`, so this comparison is the actual check.
        if not target.is_relative_to(self.root):
            log.error("storage.path_traversal_blocked", key=key)
            raise StorageError("storage key escapes the storage root")
        return target

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        target = self._resolve(key)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file and move it into place, so a crash
            # halfway through never leaves a half-written document that later
            # extracts into garbage.
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)

        await asyncio.to_thread(_write)
        return key

    async def get(self, key: str) -> bytes:
        target = self._resolve(key)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError as exc:
            raise StorageError(f"object not found: {key}") from exc

    async def delete(self, key: str) -> None:
        target = self._resolve(key)

        def _unlink() -> None:
            target.unlink(missing_ok=True)
            # Tidy the per-document directory, but never the tenant root.
            parent = target.parent
            try:
                if parent != self.root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

        await asyncio.to_thread(_unlink)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._resolve(key).is_file)
