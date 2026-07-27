"""The seam between the code registry and the stored use-case sets.

``sync_metrics`` mirrors the registered metric *names* into the ``metrics`` table and
keeps the ``default`` use case scoring all of them. Every *other* use case is user data
(``POST /use-cases``), so these tests build those links directly and check that
``metrics_for``/``resolve`` read them back. Nothing here is ever deleted — a metric
dropped from the registry may still be referenced by a stored set and by historical
``metric_scores``.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import MetricDefinition, UseCase, UseCaseMetric
from scorekeeper.db.repositories import use_cases as repo
from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.metrics.selection import (
    DEFAULT_USE_CASE,
    metrics_for,
    resolve,
    sync_metrics,
)

CATALOG_NAMES = {
    "contextual_precision",
    "hallucination",
    "faithfulness_ragas",
    "faithfulness_deepeval",
}


class _Nueva(Metric):
    """A metric added to the registry after the first sync."""

    name: ClassVar[str] = "nueva"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


async def _link(session: AsyncSession, use_case_name: str, metric_names: list[str]) -> UseCase:
    """Create a use case scoring ``metric_names``, reusing existing ``metrics`` rows."""
    use_case = UseCase(name=use_case_name)
    session.add(use_case)
    await session.flush()
    for metric_name in metric_names:
        metric = (
            await session.execute(
                select(MetricDefinition).where(MetricDefinition.name == metric_name)
            )
        ).scalars().one_or_none()
        if metric is None:
            metric = MetricDefinition(name=metric_name)
            session.add(metric)
            await session.flush()
        session.add(UseCaseMetric(use_case_id=use_case.id, metric_id=metric.id))
    await session.commit()
    return use_case


async def test_sync_materializes_the_registry_catalog(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    await db_session.commit()

    names = set((await db_session.execute(select(MetricDefinition.name))).scalars())
    assert names == CATALOG_NAMES


async def test_sync_seeds_default_scoring_every_registered_metric(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    await db_session.commit()

    default = (
        await db_session.execute(select(UseCase).where(UseCase.name == DEFAULT_USE_CASE))
    ).scalars().one()
    # An upload that names no use case still gets the full evaluation.
    assert set(await metrics_for(db_session, default.id)) == CATALOG_NAMES


async def test_sync_adds_a_new_metric_to_default(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    await db_session.commit()

    MetricRegistry.add(_Nueva)
    await sync_metrics(db_session)
    await db_session.commit()

    # A metric added to the catalog joins "default" on the next sync — no migration.
    default_id = (await repo.use_case_ids(db_session))[DEFAULT_USE_CASE]
    assert set(await metrics_for(db_session, default_id)) == CATALOG_NAMES | {"nueva"}


async def test_sync_leaves_other_use_cases_alone(
    db_session: AsyncSession, registered_metrics
) -> None:
    use_case = await _link(db_session, "soporte", ["contextual_precision"])

    await sync_metrics(db_session)
    await db_session.commit()

    # Only "default" is reconciled; a set composed through the API owns its metrics.
    assert await metrics_for(db_session, use_case.id) == ["contextual_precision"]


async def test_sync_is_idempotent(db_session: AsyncSession, registered_metrics) -> None:
    await sync_metrics(db_session)
    await db_session.commit()
    count_1 = (
        await db_session.execute(select(func.count()).select_from(MetricDefinition))
    ).scalar_one()

    await sync_metrics(db_session)
    await db_session.commit()
    count_2 = (
        await db_session.execute(select(func.count()).select_from(MetricDefinition))
    ).scalar_one()

    assert count_1 == count_2


async def test_sync_keeps_metrics_the_registry_no_longer_declares(
    db_session: AsyncSession, registered_metrics
) -> None:
    db_session.add(MetricDefinition(name="metrica_retirada"))
    await db_session.commit()

    await sync_metrics(db_session)
    await db_session.commit()

    # Insert-only: a stored set and historical metric_scores may still reference it.
    names = set((await db_session.execute(select(MetricDefinition.name))).scalars())
    assert "metrica_retirada" in names


async def test_metrics_for_reads_the_linked_set(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    use_case = await _link(
        db_session, "soporte", ["hallucination", "contextual_precision"]
    )

    assert await metrics_for(db_session, use_case.id) == [
        "contextual_precision",
        "hallucination",
    ]


async def test_resolve_returns_metric_instances(
    db_session: AsyncSession, registered_metrics
) -> None:
    await sync_metrics(db_session)
    use_case = await _link(db_session, "soporte", ["contextual_precision"])

    metrics = await resolve(db_session, use_case.id)
    assert all(isinstance(metric, Metric) for metric in metrics)
    assert {metric.name for metric in metrics} == {"contextual_precision"}


async def test_resolve_raises_on_unknown_stored_metric(
    db_session: AsyncSession, registered_metrics
) -> None:
    use_case = await _link(db_session, "raro", ["fantasma"])

    with pytest.raises(KeyError, match="Métrica desconocida"):
        await resolve(db_session, use_case.id)
