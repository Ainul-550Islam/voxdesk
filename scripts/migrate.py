"""Controlled database migration runner (Step 8 deployment hardening).

Runs ``alembic upgrade head`` under a PostgreSQL advisory lock, so that N API
containers starting at once — a scale-out, a rolling restart, two deploy jobs
racing — serialize on the lock instead of racing each other's DDL and
corrupting the schema.

How it works
------------
A session-level advisory lock is held on a dedicated connection while alembic
runs in a subprocess on its own connection. The first starter acquires the
lock (``pg_try_advisory_lock``) and migrates; every other starter polls and
waits, then migrates (a no-op when the schema is already at head) or proceeds.
If the lock cannot be acquired within ``MIGRATE_LOCK_TIMEOUT_SECONDS``
(default 600s), the starter fails loudly rather than running DDL blind.

This is the only sanctioned way the API entrypoint applies migrations. It is
PostgreSQL-only by design: the development/test path (SQLite, ``create_all``)
never calls it.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

#: Fixed, arbitrary key identifying "the VoxDesk schema migration lock". It
#: only needs to be constant across starters, not secret.
MIGRATION_LOCK_KEY = 7272_0011

#: How long to wait for a concurrent migrator to finish before failing.
_LOCK_TIMEOUT_SECONDS = int(os.environ.get("MIGRATE_LOCK_TIMEOUT_SECONDS", "600") or 600)

#: Poll interval while waiting for the lock.
_LOCK_POLL_SECONDS = 2.0


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("migrate: DATABASE_URL is not set; refusing to run", file=sys.stderr)
        sys.exit(2)
    # asyncpg speaks `postgresql://`, not the `+asyncpg` scheme used by
    # SQLAlchemy. Strip the driver suffix for the lock connection only.
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _acquire_lock(conn) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        got = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY
        )
        if got:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "could not acquire the migration lock within "
                f"{_LOCK_TIMEOUT_SECONDS}s; another migrator may be stuck"
            )
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _run(dsn: str) -> int:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await _acquire_lock(conn)
        print("migrate: lock acquired, running `alembic upgrade head`", file=sys.stderr)
        # Alembic opens its own connection via app.core.config settings (which
        # read DATABASE_URL from the environment this subprocess inherits).
        return subprocess.call(
            [sys.executable, "-m", "alembic", "upgrade", "head"]
        )
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        finally:
            await conn.close()


def main() -> int:
    try:
        return asyncio.run(_run(_dsn()))
    except TimeoutError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
