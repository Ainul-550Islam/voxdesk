"""
Database session factory.

The engine is created lazily. Building it at import time means every module
that merely *mentions* the database drags in the asyncpg driver, which breaks
unit tests, CLI scripts and any tooling that does not talk to Postgres. A
module-level `__getattr__` keeps the familiar `from app.db.session import
engine` import working while deferring the actual connection setup.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from app.core.config import settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,     # drop dead connections instead of erroring mid-call
        pool_size=10,
        max_overflow=20,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(), class_=AsyncSession, expire_on_commit=False
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request, always closed."""
    async with get_sessionmaker()() as session:
        yield session


def __getattr__(name: str):
    """Backwards-compatible lazy attributes: `engine`, `SessionLocal`."""
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_sessionmaker()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

