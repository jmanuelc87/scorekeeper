"""Queries over ``scenario_results``."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import PlatformExecution, ScenarioResult, Turn


async def get_scenario_with_turns(
    session: AsyncSession, scenario_id: str
) -> ScenarioResult | None:
    """Load a ``ScenarioResult`` by its string id, eager-loading turns + metric scores.

    Returns ``None`` for an unknown/invalid id. Eager-loads the turn tree (turns
    ordered by ``turn_number``, each with its metric scores) so serialization does
    not N+1.
    """
    try:
        key = uuid.UUID(scenario_id)
    except ValueError:
        return None
    stmt = (
        select(ScenarioResult)
        .where(ScenarioResult.id == key)
        .options(selectinload(ScenarioResult.turns).selectinload(Turn.metric_scores))
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

    ``ScenarioResult`` carries no ``run_id``; it reaches its run through
    ``platform_execution_id``, hence the join. ``platform`` and ``status`` are exact
    matches and AND-combined with the run. Callers pass an already-parsed ``run_key``
    because the run's existence is checked first (a caller cannot tell an unknown run
    from a run with no matching scenarios otherwise).

    ``use_case`` and ``platform_execution`` are eager-loaded because the projection
    reads ``use_case.name`` and ``platform_execution.platform``, and a lazy load under
    an ``AsyncSession`` is a ``MissingGreenlet``, not a slow query.
    """
    stmt = (
        select(ScenarioResult)
        .join(ScenarioResult.platform_execution)
        .where(PlatformExecution.run_id == run_key)
        .order_by(PlatformExecution.platform, ScenarioResult.scenario_id)
        .options(
            selectinload(ScenarioResult.use_case),
            selectinload(ScenarioResult.platform_execution),
        )
    )
    if platform is not None:
        stmt = stmt.where(PlatformExecution.platform == platform)
    if status is not None:
        stmt = stmt.where(ScenarioResult.status == status)
    return list((await session.execute(stmt)).scalars().all())
