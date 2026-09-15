"""Two-level cache with graceful degradation.

* Redis (async) when ``REDIS_URL`` is set -- shared across API workers.
* In-process dict with TTL otherwise -- correct for single-process deployments
  and the test suite, and it keeps every call site working with zero config.

Every operation degrades to a miss (never an exception) on backend failure, so
a cache outage slows nothing down and breaks nothing. Values are JSON-safe
(anything ``json.dumps`` can serialise).
"""
from __future__ import annotations

import inspect
import json
import time
from typing import Any

from app.core.config import settings

_DEFAULT_TTL = 60


class _MemoryCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}

    async def ping(self) -> bool:
        """In-process cache is always "reachable" by construction."""
        return True

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, raw = entry
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (time.time() + ttl, json.dumps(value))

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


class _RedisCache:
    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None

    def _get(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def ping(self) -> bool:
        """True when the Redis server answers PING. Never raises."""
        try:
            return bool(await self._get().ping())
        except Exception:
            return False

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._get().get(key)
        except Exception:
            return None
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self._get().set(key, json.dumps(value), ex=ttl)
        except Exception:
            return

    async def delete(self, key: str) -> None:
        try:
            await self._get().delete(key)
        except Exception:
            return


class Cache:
    def __init__(self, redis_url: str) -> None:
        self._backend = _RedisCache(redis_url) if redis_url else _MemoryCache()

    async def ping(self) -> bool:
        """Reachability of the cache backend. Used by the readiness probe."""
        return await self._backend.ping()

    async def get(self, key: str) -> Any | None:
        return await self._backend.get(key)

    async def set(self, key: str, value: Any, ttl: int = _DEFAULT_TTL) -> None:
        await self._backend.set(key, value, ttl)

    async def delete(self, key: str) -> None:
        await self._backend.delete(key)

    async def get_or_set(
        self, key: str, factory, ttl: int = _DEFAULT_TTL
    ) -> Any:
        """Return the cached value, or compute + store when absent."""
        hit = await self.get(key)
        if hit is not None:
            return hit
        # `factory` may be a plain function or an async one; normalise by
        # inspecting the *result* rather than the callable.
        value = factory()
        if inspect.isawaitable(value):
            value = await value
        await self.set(key, value, ttl)
        return value


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(settings.redis_url)
    return _cache
