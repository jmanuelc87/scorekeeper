"""Compose the metric set a use case is scored with.

A use case is the unit of metric selection: ``POST /use-cases`` names one and picks
the metrics it scores, and every uploaded conversation is ingested under exactly one
of them. The set lives in ``use_case_metrics``, so a metric is named once per use case
rather than repeated on every row.

Two invariants live here, not in the API:

* **Only registered metrics.** A set may reference metrics that exist in the code
  registry and nothing else — the scoring runner instantiates them by name, so an
  unknown one would be a run-time ``KeyError`` in the worker instead of a 422 here.
* **Nothing is ever removed.** There is no update or delete: every ``ScenarioResult``
  foreign-keys the use case it was scored under, so mutating a set would silently
  rewrite what a finished run means.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.selection import sync_metrics, sync_prompts
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import UseCase, UseCaseMetric
from scorekeeper.db.repositories import use_cases as repo


class UseCaseError(Exception):
    """Base class for use-case service errors."""


class UseCaseValidationError(UseCaseError):
    """Blank name, empty metric set, or an unregistered metric. → HTTP 422."""


class UseCaseConflictError(UseCaseError):
    """A use case with this name already exists. → HTTP 409."""


def _serialize(use_case: UseCase, metric_names: list[str]) -> dict[str, Any]:
    return {"id": str(use_case.id), "name": use_case.name, "metrics": metric_names}


def list_metrics() -> list[dict[str, Any]]:
    """The catalog a set can be composed from — a pure registry read, no database."""
    return sorted(
        (
            {
                "name": metric_cls.name,
                "category": metric_cls.category.value,
                "weight": metric_cls.weight,
                "rubric_version": metric_cls.rubric_version,
            }
            for metric_cls in MetricRegistry.all()
        ),
        key=lambda entry: entry["name"],
    )


async def list_use_cases(
    *, session: AsyncSession | None = None
) -> list[dict[str, Any]]:
    """Every use case with its metric names."""
    async with session_scope(session) as db:
        return [
            _serialize(use_case, metric_names)
            for use_case, metric_names in await repo.list_with_metrics(db)
        ]


async def create_use_case(
    name: str,
    metric_names: list[str],
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Create a use case scoring ``metric_names``; return its read view.

    Raises ``UseCaseValidationError`` for a blank name, an empty set, or a metric the
    registry does not know, and ``UseCaseConflictError`` when the name is taken.
    """
    name = name.strip()
    if not name:
        raise UseCaseValidationError("El nombre del caso de uso no puede estar vacío.")

    # Deduplicate but keep the caller's order, so a repeated name is not a 409 against
    # the (use_case_id, metric_id) unique constraint.
    requested = list(dict.fromkeys(metric_names))
    if not requested:
        raise UseCaseValidationError("Se requiere al menos una métrica.")

    registered = {metric_cls.name for metric_cls in MetricRegistry.all()}
    unknown = sorted(metric for metric in requested if metric not in registered)
    if unknown:
        raise UseCaseValidationError("Métrica(s) desconocida(s): " + ", ".join(unknown) + ".")

    async with session_scope(session) as db:
        # Materialize the catalog first: a metric added to the registry since the last
        # ingest has no ``metrics`` row yet, and it must be selectable here.
        await sync_metrics(db)
        await db.flush()
        # Prompt slots need their metric rows flushed above to point at.
        await sync_prompts(db)
        await db.flush()

        if await repo.get_by_name(db, name) is not None:
            raise UseCaseConflictError(f"El caso de uso {name!r} ya existe.")

        use_case = UseCase(name=name)
        db.add(use_case)
        await db.flush()

        ids = await repo.metric_ids(db, requested)
        for metric_name in requested:
            db.add(UseCaseMetric(use_case_id=use_case.id, metric_id=ids[metric_name]))

        await db.commit()
        return _serialize(use_case, sorted(requested))
