"""Queries over ``use_cases``, ``metrics`` and the ``use_case_metrics`` join."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import MetricDefinition, UseCase, UseCaseMetric


async def metric_names(session: AsyncSession) -> set[str]:
    """Every metric name already materialized in ``metrics``."""
    return set((await session.execute(select(MetricDefinition.name))).scalars())


async def metric_ids(session: AsyncSession, names: list[str]) -> dict[str, uuid.UUID]:
    """The ``name -> id`` of the ``metrics`` rows among ``names`` that exist."""
    stmt = select(MetricDefinition.name, MetricDefinition.id).where(
        MetricDefinition.name.in_(names)
    )
    return {name: key for name, key in await session.execute(stmt)}


async def use_case_ids(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Every use case as ``name -> id`` — also the ingest-time name validator."""
    return {name: key for name, key in await session.execute(select(UseCase.name, UseCase.id))}


async def get_by_name(session: AsyncSession, name: str) -> UseCase | None:
    """One use case by its unique name, or ``None``."""
    stmt = select(UseCase).where(UseCase.name == name)
    return (await session.execute(stmt)).scalars().one_or_none()


async def metric_names_for(session: AsyncSession, use_case_id: uuid.UUID) -> list[str]:
    """Metric names linked to ``use_case_id``, ordered by name."""
    stmt = (
        select(MetricDefinition.name)
        .join(UseCaseMetric, UseCaseMetric.metric_id == MetricDefinition.id)
        .where(UseCaseMetric.use_case_id == use_case_id)
        .order_by(MetricDefinition.name)
    )
    return list((await session.execute(stmt)).scalars())


async def list_with_metrics(session: AsyncSession) -> list[tuple[UseCase, list[str]]]:
    """Every use case with its metric names, ordered by use-case then metric name.

    One outer-joined query rather than a query per use case, and an outer join rather
    than an inner one so a use case with no metrics (``default``, freshly seeded) still
    appears.
    """
    stmt = (
        select(UseCase, MetricDefinition.name)
        .outerjoin(UseCaseMetric, UseCaseMetric.use_case_id == UseCase.id)
        .outerjoin(MetricDefinition, MetricDefinition.id == UseCaseMetric.metric_id)
        .order_by(UseCase.name, MetricDefinition.name)
    )
    grouped: dict[uuid.UUID, tuple[UseCase, list[str]]] = {}
    for use_case, metric_name in await session.execute(stmt):
        _, names = grouped.setdefault(use_case.id, (use_case, []))
        if metric_name is not None:
            names.append(metric_name)
    return list(grouped.values())
