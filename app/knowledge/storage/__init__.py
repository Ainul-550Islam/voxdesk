"""Storage backend selection."""
from __future__ import annotations

from app.core.config import settings
from app.knowledge.storage.base import (
    Storage,
    StorageError,
    build_key,
    safe_filename,
)
from app.knowledge.storage.local import LocalStorage

__all__ = [
    "Storage", "StorageError", "build_key", "safe_filename",
    "LocalStorage", "get_storage", "set_storage",
]

_storage: Storage | None = None


def _build_from_settings() -> Storage:
    backend = (settings.knowledge_storage_backend or "local").lower()
    if backend == "s3":
        from app.knowledge.storage.s3 import S3Storage
        return S3Storage()
    if backend == "local":
        return LocalStorage(settings.knowledge_local_path)
    raise StorageError(f"unknown storage backend: {backend}")


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = _build_from_settings()
    return _storage


def set_storage(storage: Storage | None) -> None:
    """Override the backend. Used by tests; None restores configuration."""
    global _storage
    _storage = storage

