"""The seam between the code-side metric registry and the stored use-case sets.

Two halves, with opposite owners. The *catalog* lives in code: each metric class
registers itself with ``@register``, and :func:`sync_metrics` mirrors those names
into the ``metrics`` table so a use-case set has a key to point at. The *sets*
are user data: ``use_cases`` and ``use_case_metrics`` are written through
``POST /use-cases`` (see :mod:`scorekeeper.core.services.use_cases`), never
derived from code. The scoring runner then reads a scenario's set back through
:func:`resolve`.

Nothing here deletes: a ``metrics`` row dropped from the registry may still be
referenced by a stored set and by historical ``metric_scores``.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import MetricDefinition, UseCase, UseCaseMetric
from scorekeeper.db.repositories import use_cases as repo
from scorekeeper.core.metrics import catalog as _catalog  # noqa: F401  (populate registry)
from scorekeeper.core.metrics.base import Metric
from scorekeeper.core.metrics.registry import MetricRegistry

# The use case an upload that names none lands on. Scores every registered metric, so
# an upload that picks no set still gets the full evaluation rather than nothing.
DEFAULT_USE_CASE = "default"


async def sync_metrics(session: AsyncSession) -> None:
    """Materialize the registered metrics and keep ``default`` scoring all of them.

    Idempotent and insert-only: adds a ``metrics`` row for every registered metric
    that has none, the ``default`` ``use_cases`` row if it is missing, and a link
    from ``default`` to every registered metric it does not already score. A metric
    added to the catalog therefore joins ``default`` on the next call, with no
    migration. Caller commits.

    ``default`` is maintained here rather than left to the migration because the
    registry is what defines "all metrics" and it changes with the code, and because
    the test suite builds its schema with ``Base.metadata.create_all``, never Alembic.

    Only ``default`` is touched — a use case created through ``POST /use-cases`` owns
    its set and is never reconciled.
    """
    registered = {metric_cls.name for metric_cls in MetricRegistry.all()}
    for name in registered - await repo.metric_names(session):
        session.add(MetricDefinition(name=name))

    default_id = (await repo.use_case_ids(session)).get(DEFAULT_USE_CASE)
    if default_id is None:
        # Assigned here rather than at flush so the links below can reference it.
        default_id = uuid.uuid4()
        session.add(UseCase(id=default_id, name=DEFAULT_USE_CASE))

    # The rows above must land before the links below can point at them.
    await session.flush()

    missing = sorted(registered - set(await repo.metric_names_for(session, default_id)))
    metric_ids = await repo.metric_ids(session, missing)
    for name in missing:
        session.add(UseCaseMetric(use_case_id=default_id, metric_id=metric_ids[name]))


async def metrics_for(session: AsyncSession, use_case_id: uuid.UUID) -> list[str]:
    """Metric names linked to ``use_case_id``, ordered by name.

    An empty list is a real answer, not a miss: a use case with no metrics scores
    nothing. Ingest rejects a use case that does not exist, so there is no unknown-id
    case to fall back from.
    """
    return await repo.metric_names_for(session, use_case_id)


async def resolve(session: AsyncSession, use_case_id: uuid.UUID) -> list[Metric]:
    """Instantiate the metrics linked to ``use_case_id``.

    Raises ``KeyError`` (Spanish message) if a stored metric name is not in the code
    registry.
    """
    return [MetricRegistry.create(name) for name in await metrics_for(session, use_case_id)]
