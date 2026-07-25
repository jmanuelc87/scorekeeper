"""Per-scenario metric selection, stored in the database.

The taxonomy lives in code: each metric declares the scenarios it applies to via
the ``@register`` decorator. ``sync_selection`` *queries those registered classes
at runtime* and materializes the ``use_case → metric`` mapping into the
``scenario_metrics`` table, which the scoring runner then reads. Keeping the
mapping in the DB makes selection queryable and editable alongside results, while
the decorator on each class stays the authoring source of truth.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.database import ScenarioMetric
from scorekeeper.metrics import catalog as _catalog  # noqa: F401  (populate registry)
from scorekeeper.metrics.base import Metric
from scorekeeper.metrics.registry import MetricRegistry

# Reserved use_case that applies when a scenario has no explicit metric rows.
DEFAULT_USE_CASE = "default"


def declared_selection() -> set[tuple[str, str]]:
    """Derive the desired ``(use_case, metric_name)`` pairs from the registry.

    A metric with no declared scenarios belongs to the ``default`` set.
    """
    pairs: set[tuple[str, str]] = set()
    for metric_cls in MetricRegistry.all():
        use_cases = metric_cls.scenarios or (DEFAULT_USE_CASE,)
        for use_case in use_cases:
            pairs.add((use_case, metric_cls.name))
    return pairs


async def sync_selection(session: AsyncSession) -> None:
    """Reconcile ``scenario_metrics`` with the metrics' declared scenarios.

    Idempotent: inserts missing rows and removes rows no longer declared, so the
    table always reflects the current code taxonomy. Caller commits.
    """
    desired = declared_selection()
    existing = {
        (row.use_case, row.metric_name)
        for row in (await session.execute(select(ScenarioMetric))).scalars()
    }

    for use_case, metric_name in desired - existing:
        session.add(ScenarioMetric(use_case=use_case, metric_name=metric_name))

    for use_case, metric_name in existing - desired:
        await session.execute(
            delete(ScenarioMetric).where(
                ScenarioMetric.use_case == use_case,
                ScenarioMetric.metric_name == metric_name,
            )
        )


async def metrics_for(session: AsyncSession, use_case: str) -> list[str]:
    """Metric names selected for ``use_case``, falling back to the default set."""
    names = await _query_names(session, use_case)
    if not names:
        names = await _query_names(session, DEFAULT_USE_CASE)
    return names


async def resolve(session: AsyncSession, use_case: str) -> list[Metric]:
    """Instantiate the metrics selected for ``use_case``.

    Raises ``KeyError`` (Spanish message) if a stored ``metric_name`` is not in
    the code registry.
    """
    return [MetricRegistry.create(name) for name in await metrics_for(session, use_case)]


def parse_use_cases(raw: str) -> list[str]:
    """Split a scenario's comma-separated ``use_case`` into clean, non-empty tokens."""
    return [token.strip() for token in raw.split(",") if token.strip()]


async def metrics_for_scenario(session: AsyncSession, use_case: str) -> list[str]:
    """Union of metric names across a scenario's comma-separated ``use_case`` tokens.

    Each token is resolved independently (no per-token default); the results are
    unioned, deduplicated, and kept in a deterministic order (token order, then
    metric name). Falls back to the default set only when *no* token matched any
    rows.
    """
    names: list[str] = []
    seen: set[str] = set()
    for token in parse_use_cases(use_case):
        for name in await _query_names(session, token):
            if name not in seen:
                seen.add(name)
                names.append(name)
    if not names:
        names = await _query_names(session, DEFAULT_USE_CASE)
    return names


async def resolve_scenario(session: AsyncSession, use_case: str) -> list[Metric]:
    """Instantiate the metrics for a comma-separated scenario ``use_case``.

    Raises ``KeyError`` (Spanish message) if a stored ``metric_name`` is not in
    the code registry.
    """
    return [MetricRegistry.create(name) for name in await metrics_for_scenario(session, use_case)]


async def _query_names(session: AsyncSession, use_case: str) -> list[str]:
    stmt = (
        select(ScenarioMetric.metric_name)
        .where(ScenarioMetric.use_case == use_case)
        .order_by(ScenarioMetric.metric_name)
    )
    return list((await session.execute(stmt)).scalars())
