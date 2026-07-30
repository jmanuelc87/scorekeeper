"""The seam between the code-side metric registry and the stored use-case sets.

Two halves, with opposite owners. The *catalog* lives in code: each metric class
registers itself with ``@register``, and :func:`sync_metrics` mirrors those names
into the ``metrics`` table so a use-case set has a key to point at. The *sets*
are user data: ``use_cases`` and ``use_case_metrics`` are written through
``POST /use-cases`` (see :mod:`scorekeeper.core.services.use_cases`), never
derived from code. The scoring runner then reads a scenario's set back through
:func:`resolve`.

:func:`sync_prompts` applies the same split one level down: a metric's prompt *slots*
are code, the text of each is user data seeded from the slot's default template.

Nothing here deletes: a ``metrics`` row dropped from the registry may still be
referenced by a stored set and by historical ``metric_scores``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import (
    MetricDefinition,
    Prompt,
    PromptVersion,
    UseCase,
    UseCaseMetric,
)
from scorekeeper.db.repositories import prompts as prompt_repo
from scorekeeper.db.repositories import use_cases as repo
from scorekeeper.core.metrics import catalog as _catalog  # noqa: F401  (populate registry)
from scorekeeper.core.metrics.base import Metric
from scorekeeper.core.metrics.registry import MetricRegistry

logger = logging.getLogger(__name__)

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


async def sync_prompts(session: AsyncSession) -> None:
    """Reconcile the code-owned half of each metric's prompt slots. Caller commits.

    Slug, required variables and description are code; the *text* is not, and this
    function never writes a ``prompt_versions`` row. It inserts a ``prompts`` row for a
    declared slot that has none, and refreshes the contract columns on one that exists —
    a metric that starts filling a new variable would otherwise leave the editor reading
    a stale contract.

    **A slot created here has no text.** The prompt-catalog migration seeds a published
    v1 for every slot that existed at that revision; a slot declared *afterwards* shows
    up in ``GET /prompts`` with ``active_version: null`` until a migration or the publish
    API supplies one, and scoring refuses to run for its metric until then
    (:func:`active_templates`). That is the honest consequence of the database owning the
    text — silently inventing a default in code is exactly what this design removed.

    Runs after ``sync_metrics`` because a slot needs its metric's row to point at.
    """
    metric_ids = await repo.metric_ids(
        session, sorted(metric_cls.name for metric_cls in MetricRegistry.all())
    )
    existing = await prompt_repo.slots_by_key(session)

    for metric_cls in MetricRegistry.all():
        metric_id = metric_ids.get(metric_cls.name)
        if metric_id is None:  # sync_metrics has not been flushed yet
            continue
        for slot in metric_cls.prompts:
            required = list(slot.required_variables)
            row = existing.get((metric_id, slot.slug))
            if row is None:
                session.add(
                    Prompt(
                        metric_id=metric_id,
                        slug=slot.slug,
                        required_variables=required,
                        description=slot.description,
                    )
                )
                continue
            if list(row.required_variables or []) != required:
                # Tightening the contract can strand an already-published version that no
                # longer satisfies it. Warn rather than refuse: the row must track the
                # code, and the publish gate re-validates on the next edit.
                logger.warning(
                    "Las variables requeridas de %s.%s cambian de %s a %s; "
                    "revisa que la versión activa siga siendo válida.",
                    metric_cls.name,
                    slot.slug,
                    row.required_variables,
                    required,
                )
                row.required_variables = required
            row.description = slot.description


async def metrics_for(session: AsyncSession, use_case_id: uuid.UUID) -> list[str]:
    """Metric names linked to ``use_case_id``, ordered by name.

    An empty list is a real answer, not a miss: a use case with no metrics scores
    nothing. Ingest rejects a use case that does not exist, so there is no unknown-id
    case to fall back from.
    """
    return await repo.metric_names_for(session, use_case_id)


class MissingPromptError(RuntimeError):
    """A metric about to score has a declared slot with no active published version."""


async def active_templates(
    session: AsyncSession, metric_names: Iterable[str]
) -> dict[str, dict[str, PromptVersion]]:
    """The active published version of every slot the named metrics declare.

    Returned as ``metric name -> slug -> PromptVersion``. Raises
    :class:`MissingPromptError` listing **every** unsatisfied slot at once, rather than
    dying on the first: a fresh deployment that forgot to migrate wants one message
    naming all of them.

    This is the enforcement point on purpose. The alternative — letting
    ``Metric.prompt`` raise during ``evaluate`` — surfaces inside a judge worker thread,
    where the runner's skip-metric-continue (``core.runner._evaluate_metrics``) logs a
    warning and drops the metric. A run would then finish "successfully" having silently
    scored nothing, and non-deterministically at that: ``hallucination`` returns early
    without touching its prompt when a turn has no retrieved context.
    """
    names = sorted(set(metric_names))
    if not names:
        return {}

    stored = await prompt_repo.active_versions_for(session, names)
    return _resolve_slots(stored, names)


async def bound_templates(
    session: AsyncSession, run_id: uuid.UUID, metric_names: Iterable[str]
) -> dict[str, dict[str, PromptVersion]] | None:
    """The versions ``run_id`` is already pinned to, or ``None`` when it has none yet.

    The resume counterpart of :func:`active_templates`. A run is pinned once, at the
    start; every later delivery reads the pinning back through here instead of resolving
    the *active* versions again, so a prompt published mid-run cannot split one run's
    rollups across two rubrics.

    Validated through the same :func:`_resolve_slots` as the active path, and for the
    same reason: a deploy that adds a slot to a metric mid-run would otherwise hand it a
    partial template map, and the failure would surface inside a judge worker thread
    where skip-metric-continue swallows it.
    """
    names = sorted(set(metric_names))
    if not names:
        return None

    stored = await prompt_repo.bound_versions_for_run(session, run_id)
    if not stored:
        return None
    return _resolve_slots(stored, names)


def _resolve_slots(
    stored: Mapping[tuple[str, str], PromptVersion], names: list[str]
) -> dict[str, dict[str, PromptVersion]]:
    """Group ``(metric, slug) -> version`` by metric, checking every declared slot is there.

    Raises :class:`MissingPromptError` listing **every** unsatisfied slot at once, rather
    than dying on the first: a fresh deployment that forgot to migrate wants one message
    naming all of them.
    """
    resolved: dict[str, dict[str, PromptVersion]] = {}
    missing: list[str] = []
    for name in names:
        slots = MetricRegistry.get(name).prompts
        bound = {slug: version for (metric, slug), version in stored.items() if metric == name}
        missing += [f"{name}.{slot.slug}" for slot in slots if slot.slug not in bound]
        resolved[name] = bound

    if missing:
        raise MissingPromptError(
            "No hay versión publicada y activa para: "
            + ", ".join(sorted(missing))
            + ". Ejecuta las migraciones o publica una versión para cada prompt."
        )
    return resolved


async def resolve(
    session: AsyncSession,
    use_case_id: uuid.UUID,
    templates: Mapping[str, dict[str, PromptVersion]] | None = None,
) -> list[Metric]:
    """Instantiate the metrics linked to ``use_case_id``, with their prompts bound.

    ``templates`` is the map :func:`active_templates` returns. Passing it in is how a
    whole run is pinned to one set of versions — resolved once in ``score_run`` so an
    edit landing mid-job cannot split a run's rollups across two rubrics. Omitted, this
    resolves per call, which is fine for a caller scoring nothing.

    Raises ``KeyError`` (Spanish message) if a stored metric name is not in the code
    registry, and :class:`MissingPromptError` if a declared slot has no active version.
    """
    names = await metrics_for(session, use_case_id)
    if templates is None:
        templates = await active_templates(session, names)
    return [
        MetricRegistry.create(
            name, {slug: version.template for slug, version in templates.get(name, {}).items()}
        )
        for name in names
    ]
