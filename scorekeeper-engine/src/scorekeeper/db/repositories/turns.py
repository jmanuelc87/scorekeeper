"""Queries over ``turns``."""

from __future__ import annotations

import uuid
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import MetricScore, PlatformExecution, ScenarioResult, Turn


async def _load(db: AsyncSession, turn_id: str, *options) -> Turn | None:
    """Load a ``Turn`` by its string id, or ``None`` for an unknown/invalid id."""
    try:
        key = uuid.UUID(turn_id)
    except ValueError:
        return None
    stmt = select(Turn).where(Turn.id == key).options(*options)
    return (await db.execute(stmt)).scalars().one_or_none()


async def get_turn_with_traces(session: AsyncSession, turn_id: str) -> Turn | None:
    """Load a turn with its metric scores and each score's structured trace."""
    return await _load(
        session, turn_id, selectinload(Turn.metric_scores).selectinload(MetricScore.trace)
    )


async def get_turn_with_token_usage(session: AsyncSession, turn_id: str) -> Turn | None:
    """Load a turn with its judge token usage."""
    return await _load(session, turn_id, selectinload(Turn.token_usage))


class TurnRef(NamedTuple):
    """One selected turn's place in the run — enough to know where its boundaries are.

    Two boundaries, innermost first: a turn ends its platform execution, and the last
    execution of a scenario ends the scenario.
    """

    turn_id: uuid.UUID
    platform_execution_id: uuid.UUID
    scenario_result_id: uuid.UUID


async def get_run_key_for_turn(session: AsyncSession, turn_id: str) -> uuid.UUID | None:
    """The id of the run ``turn_id`` belongs to, or ``None`` for an unknown/invalid id.

    Three ids up the tree in one row, so a per-turn job can take its run-scoped lock
    before loading anything heavier.
    """
    try:
        key = uuid.UUID(turn_id)
    except ValueError:
        return None
    stmt = (
        select(ScenarioResult.run_id)
        .join(PlatformExecution, PlatformExecution.scenario_result_id == ScenarioResult.id)
        .join(Turn, Turn.platform_execution_id == PlatformExecution.id)
        .where(Turn.id == key)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_selected_turn_refs(
    session: AsyncSession, run_key: uuid.UUID
) -> list[TurnRef]:
    """Every selected turn of a run, in the order the chain must score them.

    This is where "the next unit of work" is defined. The order is spelled out here
    rather than taken from the relationships because none of them gives a total one:
    ``BenchmarkRun.scenario_results`` orders by ``scenario_id``, which repeats when two
    scenarios share a label, and ``ScenarioResult.platform_executions`` by ``platform``,
    which repeats when a scenario holds two captures of one platform.

    Scenario-major, then platform: a scenario's platforms are scored back to back, which
    is what makes its cross-platform rollup land as soon as the scenario is done. The
    ``id`` tiebreakers keep the order total.

    The returned list answers everything a per-turn job asks: where it is, what comes
    next, and whether the successor crosses a platform-execution or scenario boundary
    (i.e. whether it owes a roll-up before handing over).
    """
    stmt = (
        select(Turn.id, PlatformExecution.id, ScenarioResult.id)
        .join(PlatformExecution, Turn.platform_execution_id == PlatformExecution.id)
        .join(ScenarioResult, PlatformExecution.scenario_result_id == ScenarioResult.id)
        .where(ScenarioResult.run_id == run_key, Turn.is_selected)
        .order_by(
            ScenarioResult.scenario_id,
            ScenarioResult.id,
            PlatformExecution.platform,
            PlatformExecution.id,
            Turn.turn_number,
        )
    )
    return [TurnRef(*row) for row in await session.execute(stmt)]


async def get_turn_for_scoring(session: AsyncSession, turn_id: str) -> Turn | None:
    """Load one turn with everything a per-turn job needs, in a single call.

    Under an ``AsyncSession`` an unloaded relationship is a ``MissingGreenlet``, not a
    slow query, so the whole reach is eager: the turn's own children to score it,
    ``platform_execution.turns`` to rebuild the conversation history *and* roll the
    execution up, and the scenario above it (with its other executions) to roll *that*
    up in turn. ``MetricScore.trace`` is required rather than an optimization — clearing
    ``turn.metric_scores`` cascades delete-orphan into it at flush time.
    """
    return await _load(
        session,
        turn_id,
        selectinload(Turn.metric_scores).selectinload(MetricScore.trace),
        selectinload(Turn.retrieved_documents),
        selectinload(Turn.token_usage),
        selectinload(Turn.platform_execution).options(
            selectinload(PlatformExecution.turns),
            selectinload(PlatformExecution.scenario_result).options(
                selectinload(ScenarioResult.use_case),
                selectinload(ScenarioResult.platform_executions),
            ),
        ),
    )
