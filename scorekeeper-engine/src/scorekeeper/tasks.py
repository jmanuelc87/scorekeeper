"""Celery tasks and the enqueue seam the API calls.

The pipeline orchestrator ``run_pipeline_task`` loads a persisted (already-ingested) run by id
and runs the two phases in order — **retrieval** (``services.retrieval.retrieve_run``:
fetch/extract each turn's source context into ``retrieved_documents``) then **evaluation**
(``services.scoring.score_run``: LLM-as-a-judge scoring). Each phase owns its own database session;
the payload is just the ``run_id`` string — everything else is read from Postgres.
``score_run_task`` is kept for scoring a run on its own.

``enqueue_run`` is a thin wrapper so callers (the API, tests) enqueue through one
monkeypatchable function instead of touching the Celery task object directly.
"""

from __future__ import annotations

import asyncio
import logging

from celery.signals import worker_process_init

from scorekeeper.core.services import retrieval, scoring
from scorekeeper.celery_app import celery_app
from scorekeeper.db.connection import engine

logger = logging.getLogger(__name__)


@worker_process_init.connect
def _reset_engine_pool(**_kwargs: object) -> None:
    """Never let a forked worker inherit the parent's pooled connections.

    The engine is built at import time and Celery's prefork pool forks after importing
    this module. ``dispose(close=False)`` drops the pool's references without closing the
    sockets — they belong to the parent process, so closing them here would break it.
    Called on the sync facade because there is no event loop yet at fork time.
    """
    engine.sync_engine.dispose(close=False)


async def _run_phases(run_id: str, *, retrieve: bool) -> None:
    """Await the requested phases, then drain the pool before the loop closes.

    asyncpg connections are bound to the event loop that opened them, and the
    ``asyncio.run`` around each task closes its loop on return — so a pooled connection
    left behind would be unusable on the next task. Disposing here (inside the task's own
    loop, not after it) is what keeps the worker reusable.
    """
    try:
        if retrieve:
            await retrieval.retrieve_run(run_id)  # phase 1: retrieval
        await scoring.score_run(run_id)  # phase 2: evaluation
    finally:
        await engine.dispose()


@celery_app.task(name="scorekeeper.run_pipeline", acks_late=True)
def run_pipeline_task(run_id: str) -> None:
    """Run retrieval then scoring for the ingested run ``run_id`` (the worker owns sessions).

    Retrieval runs first so scoring sees the fetched/extracted context; with ``acks_late`` a
    crashed worker re-runs the whole job (retrieval is idempotent via the fetch cache).
    Celery tasks are synchronous, so the async pipeline runs under its own event loop.
    """
    logger.info("Tarea recibida: pipeline para run %s", run_id)
    asyncio.run(_run_phases(run_id, retrieve=True))
    logger.info("Tarea completada: run %s", run_id)


@celery_app.task(name="scorekeeper.score_run", acks_late=True)
def score_run_task(run_id: str) -> None:
    """Score the already-ingested run ``run_id`` (the worker owns the session)."""
    logger.info("Tarea recibida: puntuar run %s", run_id)
    asyncio.run(_run_phases(run_id, retrieve=False))
    logger.info("Tarea completada: run %s", run_id)


def enqueue_run(run_id: str) -> None:
    """Enqueue the retrieval+scoring pipeline for ``run_id`` onto the Celery queue."""
    logger.info("Encolando pipeline para run %s", run_id)
    run_pipeline_task.delay(run_id)
