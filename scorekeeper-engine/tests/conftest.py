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

from scorekeeper.db.models import Base, MetricDefinition, UseCase, UseCaseMetric
from scorekeeper.core import runner
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


@pytest.fixture
def compose_use_case(session: AsyncSession):
    """Create a use case scoring the named metrics — what ``POST /use-cases`` does.

    Metric selection is user data: no metric declares a use case, so a scenario scores
    nothing until something links the two. Tests that ingest under ``"default"`` call
    this first; ``sync_metrics`` then finds both rows present and inserts nothing.
    """

    async def _compose(metric_names: list[str], use_case: str = "default") -> UseCase:
        row = UseCase(name=use_case)
        session.add(row)
        await session.flush()
        for metric_name in metric_names:
            metric = MetricDefinition(name=metric_name)
            session.add(metric)
            await session.flush()
            session.add(UseCaseMetric(use_case_id=row.id, metric_id=metric.id))
        await session.commit()
        return row

    return _compose


@pytest.fixture(autouse=True)
def _no_typesafe_key(monkeypatch) -> None:
    """Keep a developer's exported ``TYPESAFE_API_KEY`` out of every ``Settings()``.

    It is the variable the TypeSafe SDK itself reads, so it is commonly exported; left
    in place it would make ``make_judge`` wrap every judge a test builds in a
    ``TypesafeJudge``. Tests that want the wrapper pass the key explicitly.
    """
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


#: The genuine pacing seams, captured before ``_no_turn_delay`` ever patches them.
_REAL_PACE_BETWEEN_TURNS = EvalRunner._pace_between_turns
_REAL_TURN_DELAY_SECONDS = runner.turn_delay_seconds


@pytest.fixture(autouse=True)
def _no_turn_delay(monkeypatch) -> None:
    """Skip the between-turns pacing so scoring tests stay fast and deterministic.

    Patches the method rather than ``asyncio.sleep`` — ``scorekeeper.core.runner.asyncio`` is
    the global asyncio module, so patching ``.sleep`` on it would also patch it for
    pytest-asyncio's own machinery.

    Both seams are neutralized, because the two execution models pace differently: the
    in-process runner *sleeps* the delay, while the chained per-turn jobs *return* it as
    a Celery countdown. Left unpatched, the latter would hand back a real random interval
    and any assertion on it would be flaky.
    """

    async def _instant(self: EvalRunner) -> None:
        return None

    monkeypatch.setattr(EvalRunner, "_pace_between_turns", _instant)
    monkeypatch.setattr(runner, "turn_delay_seconds", lambda: 0.0)


@pytest.fixture
def real_turn_pacing(monkeypatch) -> list[float]:
    """Undo ``_no_turn_delay`` and record what the pacing would have slept.

    For the tests that assert on the pacing itself. Returns the list the recorded
    delays land in; nothing actually sleeps.
    """
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(runner, "turn_delay_seconds", _REAL_TURN_DELAY_SECONDS)
    monkeypatch.setattr(EvalRunner, "_pace_between_turns", _REAL_PACE_BETWEEN_TURNS)
    monkeypatch.setattr("asyncio.sleep", _record)
    return slept
