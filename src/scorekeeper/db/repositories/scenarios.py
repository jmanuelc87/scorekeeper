"""Queries over ``scenario_results``."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import ScenarioResult, Turn


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
