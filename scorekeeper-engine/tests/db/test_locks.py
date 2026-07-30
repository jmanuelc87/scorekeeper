"""Tests for the run-scoped advisory lock in ``db.connection``.

Advisory locks are a PostgreSQL feature; the suite runs on SQLite. What is asserted
here is the degradation: outside PostgreSQL the lock must grant unconditionally and
must not touch the connection pool at all — the suite shares one DBAPI connection
through a ``StaticPool``, so a second one would deadlock rather than degrade.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.connection import run_lock


async def test_run_lock_is_granted_outside_postgresql(session: AsyncSession) -> None:
    async with run_lock(session, uuid.uuid4()) as acquired:
        assert acquired is True


class _ExplodingBind:
    """A bind whose only behaviour is to fail if anyone opens a connection on it."""

    class dialect:  # noqa: N801 - mirrors SQLAlchemy's attribute, not a class name
        name = "sqlite"

    def connect(self):  # pragma: no cover - reaching this is the failure
        raise AssertionError("run_lock abrió una conexión fuera de PostgreSQL")


class _FakeSession:
    def __init__(self, bind) -> None:
        self.bind = bind


async def test_run_lock_opens_no_connection_outside_postgresql() -> None:
    # Not just an optimization: the suite shares one DBAPI connection via StaticPool,
    # so a second connection here would deadlock instead of degrading.
    async with run_lock(_FakeSession(_ExplodingBind()), uuid.uuid4()) as acquired:
        assert acquired is True


async def test_run_lock_is_granted_when_the_session_has_no_bind() -> None:
    async with run_lock(_FakeSession(None), uuid.uuid4()) as acquired:
        assert acquired is True


async def test_run_lock_grants_twice_concurrently_outside_postgresql(
    session: AsyncSession,
) -> None:
    # No real mutual exclusion off PostgreSQL — nesting must not block or raise.
    run_key = uuid.uuid4()
    async with run_lock(session, run_key) as first:
        async with run_lock(session, run_key) as second:
            assert first is True
            assert second is True
