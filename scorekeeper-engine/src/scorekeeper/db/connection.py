"""The engine, the session factory, and the one session convention.

Kept apart from :mod:`scorekeeper.db.models` so that importing the models — as
``migrations/env.py`` does — does not construct an engine as a side effect.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from scorekeeper.config.settings import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
# expire_on_commit=False is load-bearing under asyncio: with it True, every attribute
# read after a commit is a lazy refresh — i.e. implicit IO from a context that cannot
# do it, which surfaces as MissingGreenlet rather than as a slow query.
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session: AsyncSession | None = None,
) -> AsyncGenerator[AsyncSession]:
    """Yield the injected ``session``, or open a fresh one and close it afterwards.

    The one place the "caller may inject a session, otherwise we own one" convention
    lives; every service function opens with ``async with session_scope(session) as db``.
    ``SessionLocal`` is read from the module global at call time, so tests repoint it
    once and every un-injected caller follows.
    """
    if session is not None:
        yield session
        return
    db = SessionLocal()
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def run_lock(session: AsyncSession, run_key: uuid.UUID) -> AsyncGenerator[bool]:
    """Hold a run-scoped advisory lock for the block; yield whether it was acquired.

    Single-flight for a run's chained per-turn jobs (``core.services.chain``): a duplicate
    delivery must never judge the same turn as the live chain. A caller that gets ``False``
    drops its delivery — the chain that holds the lock owns the work.

    Two deliberate choices, both about *when* the lock is released:

    * a **session-level** lock, not ``pg_try_advisory_xact_lock`` — a turn job commits
      several times, and a transaction-scoped lock would be dropped at the first commit;
    * its **own connection**, not the session's — an ``AsyncSession`` returns its
      connection to the pool on every commit, which would take the lock with it.

    Outside PostgreSQL there are no advisory locks, so this yields ``True`` and opens no
    connection at all. That is not just a convenience for the SQLite test suite: those
    tests share a single DBAPI connection through a ``StaticPool``, so opening a second
    one here would deadlock rather than degrade.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        yield True
        return
    # Postgres advisory keys are signed 64-bit; take the UUID's first 8 bytes.
    key = int.from_bytes(run_key.bytes[:8], "big", signed=True)
    async with bind.connect() as conn:
        acquired = bool(
            (await conn.execute(select(func.pg_try_advisory_lock(key)))).scalar_one()
        )
        try:
            yield acquired
        finally:
            if acquired:
                await conn.execute(select(func.pg_advisory_unlock(key)))


async def create_schema() -> None:
    # Imported here, not at module scope, so models.py never depends on this module.
    from scorekeeper.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
