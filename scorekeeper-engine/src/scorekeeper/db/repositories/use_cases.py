"""Queries over ``use_cases``, ``metrics`` and the ``use_case_metrics`` join."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

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


async def metric_names_for_many(
    session: AsyncSession, use_case_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Metric names for several use cases at once, each ordered by name.

    The batched form of :func:`metric_names_for`, for the scoring runner resolving a
    whole run's metric set up front instead of one query per scenario. Ordering matters
    downstream: the runner gathers metric results in argument order, which is what keeps
    a turn's ``MetricScore`` rows stable across runs.
    """
    keys = list(dict.fromkeys(use_case_ids))
    if not keys:  # ``in_([])`` is a SQLAlchemy warning, and there is nothing to ask.
        return {}
    stmt = (
        select(UseCaseMetric.use_case_id, MetricDefinition.name)
        .join(MetricDefinition, UseCaseMetric.metric_id == MetricDefinition.id)
        .where(UseCaseMetric.use_case_id.in_(keys))
        .order_by(MetricDefinition.name)
    )
    grouped: dict[uuid.UUID, list[str]] = {key: [] for key in keys}
    for use_case_id, metric_name in await session.execute(stmt):
        grouped[use_case_id].append(metric_name)
    return grouped


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


async def get_by_id(session: AsyncSession, use_case_id: uuid.UUID) -> UseCase | None:
    """One use case by its id, or ``None``."""
    stmt = select(UseCase).where(UseCase.id == use_case_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def replace_metrics(
    session: AsyncSession, use_case_id: uuid.UUID, metric_ids: list[uuid.UUID]
) -> None:
    """Replace the metrics linked to a use case.

    Deletes all ``use_case_metrics`` rows for ``use_case_id`` and inserts new ones for
    each ``metric_ids``. No-op if ``metric_ids`` is empty. Caller commits.
    """
    # Delete all existing links.
    from sqlalchemy import delete

    stmt = delete(UseCaseMetric).where(UseCaseMetric.use_case_id == use_case_id)
    await session.execute(stmt)

    # Insert new links.
    for metric_id in metric_ids:
        session.add(UseCaseMetric(use_case_id=use_case_id, metric_id=metric_id))
