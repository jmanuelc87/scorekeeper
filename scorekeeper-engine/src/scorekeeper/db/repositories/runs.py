"""Queries over ``benchmark_runs`` and the run tree hanging off it."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    ScenarioResult,
    Turn,
)


def run_tree_options(*, metric_scores: bool = True, retrieval: bool = True):
    """Eager-load the whole run tree — the loader every write path uses.

    ``score_run``/``retrieve_run`` walk BenchmarkRun → PlatformExecution →
    ScenarioResult → Turn → {metric_scores(+trace), retrieved_documents, token_usage}.
    Under an ``AsyncSession`` an unloaded relationship is not a slow query but a
    ``MissingGreenlet``, so the tree is loaded up front rather than lazily.

    ``MetricScore.trace`` is required, not an optimization: removing a score from
    ``turn.metric_scores`` cascades delete-orphan into it, which the unit of work
    resolves *at flush time* — the least obvious place to take a lazy load. The
    resume diff in ``core.runner.run_turn`` drops stale rows one at a time, so this
    still holds.

    ``ScenarioResult.use_case`` comes along because ``run_scenario`` names it in its
    log line and metric selection reads its links.
    """
    turn_opts = [selectinload(Turn.token_usage)]
    if metric_scores:
        turn_opts.append(selectinload(Turn.metric_scores).selectinload(MetricScore.trace))
    if retrieval:
        turn_opts.append(selectinload(Turn.retrieved_documents))
    return (
        selectinload(BenchmarkRun.platform_executions)
        .selectinload(PlatformExecution.scenario_results)
        .options(
            selectinload(ScenarioResult.use_case),
            selectinload(ScenarioResult.turns).options(*turn_opts),
        )
    )


def _list_options(*, with_metric_scores: bool):
    """Eager-load the run tree down to the depth serialization requires (no N+1).

    Turns are loaded at *every* depth, not just for metric scores: serializing a
    run always reports turn-level progress, which counts a scenario's turns. The
    use case comes along at every depth too — ``_serialize_scenario`` emits its name.
    """
    turns = selectinload(ScenarioResult.turns)
    if with_metric_scores:
        turns = turns.selectinload(Turn.metric_scores)
    return (
        selectinload(BenchmarkRun.platform_executions)
        .selectinload(PlatformExecution.scenario_results)
        .options(turns, selectinload(ScenarioResult.use_case))
    )


async def _load(db: AsyncSession, run_id: str, *options) -> BenchmarkRun | None:
    """Load a ``BenchmarkRun`` by its string id, or ``None`` for an unknown/invalid id.

    A ``select()`` rather than ``session.get()``: get() returns an identity-map hit
    without applying loader options, so a run already in the session (``run_evaluation``
    threads one session through ingest → retrieve → score) would come back with its
    relationships unloaded and fail on first access.
    """
    try:
        key = uuid.UUID(run_id)
    except ValueError:
        return None
    stmt = select(BenchmarkRun).where(BenchmarkRun.id == key).options(*options)
    return (await db.execute(stmt)).scalars().one_or_none()


async def get_run(session: AsyncSession, run_id: str) -> BenchmarkRun | None:
    """Load a run with no relationships loaded — for callers that only read columns."""
    return await _load(session, run_id)


async def get_run_tree(
    session: AsyncSession,
    run_id: str,
    *,
    metric_scores: bool = True,
    retrieval: bool = True,
) -> BenchmarkRun | None:
    """Load a run with its whole tree eager-loaded (see :func:`run_tree_options`)."""
    return await _load(
        session, run_id, run_tree_options(metric_scores=metric_scores, retrieval=retrieval)
    )


async def list_runs(
    session: AsyncSession,
    *,
    run_key: uuid.UUID | None = None,
    platform: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    with_metric_scores: bool = False,
) -> list[BenchmarkRun]:
    """Runs matching every given filter, ordered by ``BenchmarkRun.created_at``.

    ``start``/``end`` bound the scoring window (``PlatformExecution.started_at`` /
    ``finished_at``), which stays ``NULL`` until a worker scores the run — so a
    bound naturally excludes queued and in-progress runs.
    """
    stmt = (
        select(BenchmarkRun)
        .join(BenchmarkRun.platform_executions)
        .order_by(BenchmarkRun.created_at)
        .options(_list_options(with_metric_scores=with_metric_scores))
    )
    if run_key is not None:
        stmt = stmt.where(BenchmarkRun.id == run_key)
    if platform is not None:
        stmt = stmt.where(PlatformExecution.platform == platform)
    if start is not None:
        stmt = stmt.where(PlatformExecution.started_at >= start)
    if end is not None:
        stmt = stmt.where(PlatformExecution.finished_at <= end)
    return list((await session.execute(stmt)).scalars().unique().all())
