"""Queries over ``scenario_results``."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import BenchmarkRun, PlatformExecution, ScenarioResult, Turn


async def get_scenario_with_turns(
    session: AsyncSession, scenario_id: str
) -> ScenarioResult | None:
    """Load a ``ScenarioResult`` by its string id, eager-loading turns + metric scores.

    Returns ``None`` for an unknown/invalid id. Eager-loads the whole turn tree — every
    platform execution (ordered by ``platform``), its turns (ordered by ``turn_number``)
    and their metric scores — so serialization does not N+1.
    """
    try:
        key = uuid.UUID(scenario_id)
    except ValueError:
        return None
    stmt = (
        select(ScenarioResult)
        .where(ScenarioResult.id == key)
        .options(
            selectinload(ScenarioResult.platform_executions)
            .selectinload(PlatformExecution.turns)
            .selectinload(Turn.metric_scores)
        )
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def latest_scenario_for_reuse(
    session: AsyncSession,
    scenario_id: str,
    *,
    run_status: str,
    scenario_status: str,
    run_key: uuid.UUID | None = None,
) -> ScenarioResult | None:
    """The newest scenario labelled ``scenario_id`` sitting at ``scenario_status``, whose
    run still sits at ``run_status``.

    What lets a second capture of the same scenario join the first instead of opening a
    run of its own. Both statuses are the caller's guard, not this query's opinion: only
    a run that has not started scoring, holding a scenario nothing has scored yet, can
    take extra unscored executions. The scenario check is the narrower of the two — a
    scenario that already rolled up to a scored status must not gain an execution that
    would silently invalidate that roll-up, even if its run somehow lagged behind.
    Returns ``None`` when nothing matches — the caller then creates the scenario as usual.

    ``run_key`` narrows the search to a single run. A capture that named a batch has
    already resolved its run, and the scenario it extends must be one of *that* run's —
    otherwise the label would pull in a scenario of some unrelated run, which is the
    opposite of grouping. Left ``None`` the search spans every run, as before.

    ``run`` and ``platform_executions`` are eager-loaded because the caller attaches a new
    execution to the row and reads its run, and a lazy load under an ``AsyncSession`` is a
    ``MissingGreenlet``, not a slow query.
    """
    stmt = (
        select(ScenarioResult)
        .join(ScenarioResult.run)
        .where(
            ScenarioResult.scenario_id == scenario_id,
            ScenarioResult.status == scenario_status,
            BenchmarkRun.status == run_status,
        )
    )
    if run_key is not None:
        stmt = stmt.where(ScenarioResult.run_id == run_key)
    stmt = (
        stmt.order_by(BenchmarkRun.created_at.desc())
        .limit(1)
        .options(
            selectinload(ScenarioResult.run),
            selectinload(ScenarioResult.platform_executions),
        )
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_scenarios_for_run(
    session: AsyncSession,
    run_key: uuid.UUID,
    *,
    platform: str | None = None,
    status: str | None = None,
) -> list[ScenarioResult]:
    """Every scenario of one run as a flat list, with the given filters applied.

    The run is a column on the row itself. ``platform`` selects scenarios that hold a
    matching execution — the scenario is still returned whole, with every platform it
    ran on, since a scenario filtered down to one platform is no longer a comparison.
    ``status`` matches the scenario's own rolled-up status. Both are exact and
    AND-combined with the run. Callers pass an already-parsed ``run_key`` because the
    run's existence is checked first (a caller cannot tell an unknown run from a run
    with no matching scenarios otherwise).

    ``use_case`` and ``platform_executions`` are eager-loaded because the projection
    reads ``use_case.name`` and each execution's rollup, and a lazy load under an
    ``AsyncSession`` is a ``MissingGreenlet``, not a slow query.
    """
    stmt = (
        select(ScenarioResult)
        .where(ScenarioResult.run_id == run_key)
        .order_by(ScenarioResult.scenario_id)
        .options(
            selectinload(ScenarioResult.use_case),
            selectinload(ScenarioResult.platform_executions),
        )
    )
    if platform is not None:
        stmt = stmt.where(
            ScenarioResult.platform_executions.any(PlatformExecution.platform == platform)
        )
    if status is not None:
        stmt = stmt.where(ScenarioResult.status == status)
    return list((await session.execute(stmt)).scalars().all())
