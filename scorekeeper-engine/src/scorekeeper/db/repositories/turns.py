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
    """One selected turn's place in the run — enough to know where its boundaries are."""

    turn_id: uuid.UUID
    scenario_result_id: uuid.UUID
    platform_execution_id: uuid.UUID


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
        select(PlatformExecution.run_id)
        .join(ScenarioResult, ScenarioResult.platform_execution_id == PlatformExecution.id)
        .join(Turn, Turn.scenario_result_id == ScenarioResult.id)
        .where(Turn.id == key)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_selected_turn_refs(
    session: AsyncSession, run_key: uuid.UUID
) -> list[TurnRef]:
    """Every selected turn of a run, in the order the chain must score them.

    This is where "the next unit of work" is defined. The order is spelled out here
    rather than taken from the relationships because neither gives a total one:
    ``BenchmarkRun.platform_executions`` orders by ``started_at``, which is NULL until a
    worker touches it, and ``PlatformExecution.scenario_results`` has no order at all.
    The ``platform``/``scenario_id`` pairing matches ``scenarios.list_scenarios_for_run``;
    the ``id`` tiebreakers keep it total when two scenarios share a label.

    The returned list answers everything a per-turn job asks: where it is, what comes
    next, and whether the successor crosses a scenario or platform boundary (i.e. whether
    it owes a roll-up before handing over).
    """
    stmt = (
        select(Turn.id, ScenarioResult.id, PlatformExecution.id)
        .join(ScenarioResult, Turn.scenario_result_id == ScenarioResult.id)
        .join(PlatformExecution, ScenarioResult.platform_execution_id == PlatformExecution.id)
        .where(PlatformExecution.run_id == run_key, Turn.is_selected)
        .order_by(
            PlatformExecution.platform,
            PlatformExecution.id,
            ScenarioResult.scenario_id,
            ScenarioResult.id,
            Turn.turn_number,
        )
    )
    return [TurnRef(*row) for row in await session.execute(stmt)]


async def get_turn_for_scoring(session: AsyncSession, turn_id: str) -> Turn | None:
    """Load one turn with everything a per-turn job needs, in a single call.

    Under an ``AsyncSession`` an unloaded relationship is a ``MissingGreenlet``, not a
    slow query, so the whole reach is eager: the turn's own children to score it,
    ``scenario_result.turns`` to rebuild the conversation history *and* roll the scenario
    up, and the platform execution's scenarios to roll that up in turn.
    ``MetricScore.trace`` is required rather than an optimization — clearing
    ``turn.metric_scores`` cascades delete-orphan into it at flush time.
    """
    return await _load(
        session,
        turn_id,
        selectinload(Turn.metric_scores).selectinload(MetricScore.trace),
        selectinload(Turn.retrieved_documents),
        selectinload(Turn.token_usage),
        selectinload(Turn.scenario_result).options(
            selectinload(ScenarioResult.use_case),
            selectinload(ScenarioResult.turns),
            selectinload(ScenarioResult.platform_execution).selectinload(
                PlatformExecution.scenario_results
            ),
        ),
    )
