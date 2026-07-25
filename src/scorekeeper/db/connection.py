"""The engine, the session factory, and the one session convention.

Kept apart from :mod:`scorekeeper.db.models` so that importing the models — as
``migrations/env.py`` does — does not construct an engine as a side effect.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

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


async def create_schema() -> None:
    # Imported here, not at module scope, so models.py never depends on this module.
    from scorekeeper.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
