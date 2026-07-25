"""Shared database fixtures for the whole suite.

Every DB test runs against an in-memory SQLite through aiosqlite. That is not just a
convenient stand-in for Postgres: an ``AsyncSession`` raises ``MissingGreenlet`` on a
lazy relationship access under aiosqlite exactly as it does under asyncpg, so a missing
``selectinload`` fails here rather than in production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from scorekeeper.db.models import Base
from scorekeeper.core.runner import EvalRunner


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh, empty database per test.

    ``StaticPool`` is required, not tuning: every new connection to ``:memory:`` opens a
    *different* database, so a single pooled connection is what lets code under test open
    its own ``SessionLocal()`` and still see rows the fixture wrote.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_session_local(monkeypatch, session_factory) -> None:
    """Point every un-injected ``SessionLocal()`` at the test database.

    ``database.session_scope`` resolves ``SessionLocal`` from the module global at call
    time, so this single patch covers every consumer — and guarantees no test can reach
    the real DATABASE_URL by taking a code path that opens its own session.
    """
    monkeypatch.setattr("scorekeeper.db.connection.SessionLocal", session_factory)


@pytest.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def db_session(session: AsyncSession) -> AsyncSession:
    """Alias of ``session`` under the name tests/metrics fixtures ask for."""
    return session


#: The genuine pacing method, captured before ``_no_turn_delay`` ever patches it.
_REAL_PACE_BETWEEN_TURNS = EvalRunner._pace_between_turns


@pytest.fixture(autouse=True)
def _no_turn_delay(monkeypatch) -> None:
    """Skip the between-turns pacing so scoring tests stay fast and deterministic.

    Patches the method rather than ``asyncio.sleep`` — ``scorekeeper.core.runner.asyncio`` is
    the global asyncio module, so patching ``.sleep`` on it would also patch it for
    pytest-asyncio's own machinery.
    """

    async def _instant(self: EvalRunner) -> None:
        return None

    monkeypatch.setattr(EvalRunner, "_pace_between_turns", _instant)


@pytest.fixture
def real_turn_pacing(monkeypatch) -> list[float]:
    """Undo ``_no_turn_delay`` and record what the pacing would have slept.

    For the tests that assert on the pacing itself. Returns the list the recorded
    delays land in; nothing actually sleeps.
    """
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(EvalRunner, "_pace_between_turns", _REAL_PACE_BETWEEN_TURNS)
    monkeypatch.setattr("asyncio.sleep", _record)
    return slept
