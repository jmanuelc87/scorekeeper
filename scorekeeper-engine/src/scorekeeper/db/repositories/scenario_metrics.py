"""Queries over ``scenario_metrics`` — the metric selection per use case."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import ScenarioMetric


async def list_pairs(session: AsyncSession) -> set[tuple[str, str]]:
    """Every stored ``(use_case, metric_name)`` pair."""
    return {
        (row.use_case, row.metric_name)
        for row in (await session.execute(select(ScenarioMetric))).scalars()
    }


async def delete_pair(session: AsyncSession, use_case: str, metric_name: str) -> None:
    """Remove one ``(use_case, metric_name)`` row. Caller commits."""
    await session.execute(
        delete(ScenarioMetric).where(
            ScenarioMetric.use_case == use_case,
            ScenarioMetric.metric_name == metric_name,
        )
    )


async def metric_names_for(session: AsyncSession, use_case: str) -> list[str]:
    """Metric names selected for ``use_case``, ordered by name."""
    stmt = (
        select(ScenarioMetric.metric_name)
        .where(ScenarioMetric.use_case == use_case)
        .order_by(ScenarioMetric.metric_name)
    )
    return list((await session.execute(stmt)).scalars())
