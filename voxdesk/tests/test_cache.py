"""Step 9 — cache: get/set/delete/get_or_set with the in-process backend."""
from __future__ import annotations

import pytest

from app.core.cache import Cache


@pytest.mark.asyncio
async def test_set_then_get():
    c = Cache(redis_url="")
    await c.set("a", {"x": 1}, ttl=60)
    assert await c.get("a") == {"x": 1}


@pytest.mark.asyncio
async def test_miss_returns_none():
    c = Cache(redis_url="")
    assert await c.get("never-set") is None


@pytest.mark.asyncio
async def test_delete():
    c = Cache(redis_url="")
    await c.set("a", 1)
    await c.delete("a")
    assert await c.get("a") is None


@pytest.mark.asyncio
async def test_get_or_set_computes_once():
    c = Cache(redis_url="")
    calls = []

    async def factory():
        calls.append(1)
        return "value"

    assert await c.get_or_set("k", factory) == "value"
    assert await c.get_or_set("k", factory) == "value"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expiry_returns_none():
    c = Cache(redis_url="")
    await c.set("k", "v", ttl=-1)  # already expired
    assert await c.get("k") is None
