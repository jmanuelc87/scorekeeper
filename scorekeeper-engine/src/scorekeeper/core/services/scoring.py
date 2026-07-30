"""The scoring phase — hand a persisted run to the LLM judge.

Slow; the Celery worker runs it. ``EvalRunner`` commits **per scenario** (its
atomic-write unit), so an interrupted job keeps every scenario it already
finished. On failure the run is marked ``fallido`` so pollers see a terminal
state. :func:`run_evaluation` is the synchronous ingest -> retrieve -> score
path on one session, kept for tests and in-process callers.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics import selection
from scorekeeper.core.metrics.judge import Judge
from scorekeeper.core.retrieval.protocols import RetrievalPipeline
from scorekeeper.core.runner import (
    STATUS_COMPLETADO,
    STATUS_FALLIDO,
    STATUS_PARCIAL,
    EvalRunner,
)
from scorekeeper.core.services import ingestion, retrieval
from scorekeeper.core.services.ingestion import UploadedFile
from scorekeeper.core.services.serializers import summarize_run
from scorekeeper.core.services.status import STATUS_EN_PROCESO
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import BenchmarkRun, PromptVersion
from scorekeeper.db.repositories import prompts as prompt_repo
from scorekeeper.db.repositories import runs as run_repo
from scorekeeper.db.repositories import use_cases as use_case_repo

logger = logging.getLogger(__name__)


async def score_run(
    run_id: str,
    *,
    session: AsyncSession | None = None,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """Score a previously-ingested run and persist the results.

    Loads the run by id, marks it ``en_proceso``, scores every turn with the LLM
    judge (``EvalRunner`` commits per scenario), rolls the status up, and returns the
    JSON-serializable summary. This is the slow half the Celery worker runs off the
    request path.

    ``session`` / ``judge`` default to ``SessionLocal()`` / the configured judge.
    Raises ``ValueError`` when ``run_id`` is unknown. On any scoring failure the run
    is marked ``fallido`` (a terminal state for pollers) and the error re-raised.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run_tree(db, run_id)
        if run is None:
            raise ValueError(f"El run {run_id} no existe.")

        run.status = STATUS_EN_PROCESO
        await db.commit()

        turn_total = sum(
            len(s.turns) for pe in run.platform_executions for s in pe.scenario_results
        )
        logger.info("Puntuando run %s: %d turno(s) con el juez…", run.id, turn_total)
        try:
            # Pin the prompt versions before anything scores. Resolving here rather than
            # per scenario is what makes a run internally comparable: scoring is a long
            # Celery job while the prompt API stays live, so a per-scenario resolve would
            # let an edit land mid-run and split one run's rollups across two rubrics. A
            # slot with no active version raises here — before a single judge call —
            # rather than inside a worker thread where skip-metric-continue would swallow
            # it. Inside the try so that failure still leaves the run in a terminal state
            # for pollers, like any other scoring failure.
            templates = await pin_prompts(db, run)
            await db.commit()
            # scores everything, commits
            await EvalRunner(db, judge, templates).run_benchmark(run)
        except Exception:
            # rollback expires every instance, so the reload carries the loaders again.
            await db.rollback()
            run = await run_repo.get_run_tree(db, run_id)
            if run is not None:
                run.status = STATUS_FALLIDO
                await db.commit()
            raise

        run.status = run_status(run)
        await db.commit()
        logger.info("Run %s finalizado con estado %s", run.id, run.status)
        return summarize_run(run)


async def pin_prompts(
    db: AsyncSession, run: BenchmarkRun
) -> dict[str, dict[str, PromptVersion]]:
    """Resolve the run's prompt versions and record them as its bindings.

    Walks the loaded tree for the use cases in play — ``use_case_id`` is a column on an
    already-loaded ``ScenarioResult``, so this costs no query — batches their metric
    names, and resolves each declared slot's active version.

    The bindings are the superset of what *could* score: a scenario whose turns are all
    deselected still contributes its use case. That keeps "written once at the start"
    true, which is the property that makes an interrupted run still record what it was
    scoring under. Raises ``MissingPromptError`` when a slot has no active version.

    "Once" is enforced here: a run that already carries bindings is re-pinned to those,
    not to whatever is active now. Otherwise a re-delivery — or a per-turn job — would
    re-resolve, and a prompt published mid-run would split the run's rollups across two
    rubrics, which is exactly what pinning exists to prevent.
    """
    use_case_ids = {
        scenario.use_case_id
        for platform_exec in run.platform_executions
        for scenario in platform_exec.scenario_results
    }
    by_use_case = await use_case_repo.metric_names_for_many(db, use_case_ids)
    metric_names = {name for names in by_use_case.values() for name in names}

    bound = await selection.bound_templates(db, run.id, metric_names)
    if bound is not None:
        return bound

    templates = await selection.active_templates(db, metric_names)
    await prompt_repo.replace_run_bindings(
        db,
        run.id,
        {version.id for slots in templates.values() for version in slots.values()},
    )
    return templates


async def run_evaluation(
    platform: str,
    files: list[UploadedFile],
    *,
    session: AsyncSession | None = None,
    judge: Judge | None = None,
    pipeline: RetrievalPipeline | None = None,
) -> dict[str, Any]:
    """Ingest ``files``, retrieve their context, and score them under ``platform`` in one call.

    The synchronous path: :func:`ingest_evaluation` → select every turn → :func:`retrieve_run`
    → :func:`score_run` on the same session. Because scoring is opt-in per turn (only
    ``Turn.is_selected`` turns are evaluated by the worker), this convenience selects all
    turns so it evaluates the whole file — the HTTP API instead ingests inline, lets the
    caller pick a subset via the selection endpoint, then enqueues the retrieval+scoring
    orchestrator onto the Celery worker. ``pipeline`` is injectable so tests avoid
    network/LLM calls.
    """
    async with session_scope(session) as db:
        run_id = await ingestion.ingest_evaluation(platform, files, session=db)
        await _select_all_turns(db, run_id)
        await retrieval.retrieve_run(run_id, session=db, pipeline=pipeline)
        return await score_run(run_id, session=db, judge=judge)


async def _select_all_turns(db: AsyncSession, run_id: str) -> None:
    """Mark every turn of a run selected for scoring (the "evaluate everything" default)."""
    run = await run_repo.get_run_tree(db, run_id, metric_scores=False, retrieval=False)
    if run is None:
        return
    for platform_exec in run.platform_executions:
        for scenario in platform_exec.scenario_results:
            for turn in scenario.turns:
                turn.is_selected = True
    await db.commit()


def run_status(run: BenchmarkRun) -> str:
    """Roll scenario statuses up to a run-level status.

    ``fallido`` when nothing scored or every scenario failed, ``completado`` when
    all completed, otherwise ``parcial``.
    """
    statuses = [
        scenario.status
        for platform_exec in run.platform_executions
        for scenario in platform_exec.scenario_results
    ]
    if not statuses or all(status == STATUS_FALLIDO for status in statuses):
        return STATUS_FALLIDO
    if all(status == STATUS_COMPLETADO for status in statuses):
        return STATUS_COMPLETADO
    return STATUS_PARCIAL
