"""Queries over ``platform_executions``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import BenchmarkRun, PlatformExecution


async def list_platform_executions(
    session: AsyncSession,
    *,
    platform: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[PlatformExecution]:
    """Platform executions matching every given filter, flat and ordered.

    ``start``/``end`` bound the scoring window (``started_at`` / ``finished_at``),
    which stays ``NULL`` until a worker scores the run — so a bound naturally
    excludes queued and in-progress executions, exactly as in :func:`list_runs`.

    Ordered by ``BenchmarkRun.created_at`` rather than ``started_at``: the latter is
    nullable, and NULLs sort first on SQLite but last on PostgreSQL, so the order
    would differ between the test suite and production. ``platform`` breaks ties,
    since one run may hold several executions (files can override the platform).

    Only ``scenario_results`` is eager-loaded — the projection stops at the platform
    rollup, which reads each scenario's ``status`` and the collection length. The
    join to ``run`` only exposes ``created_at`` to ``ORDER BY``; it populates no
    relationship, and ``run_id`` is a column on the selected row.
    """
    stmt = (
        select(PlatformExecution)
        .join(PlatformExecution.run)
        .order_by(BenchmarkRun.created_at, PlatformExecution.platform)
        .options(selectinload(PlatformExecution.scenario_results))
    )
    if platform is not None:
        stmt = stmt.where(PlatformExecution.platform == platform)
    if start is not None:
        stmt = stmt.where(PlatformExecution.started_at >= start)
    if end is not None:
        stmt = stmt.where(PlatformExecution.finished_at <= end)
    # No .unique() unlike list_runs: PlatformExecution.run is many-to-one, so the
    # join cannot duplicate rows, and selectinload issues a second SELECT.
    return list((await session.execute(stmt)).scalars().all())
