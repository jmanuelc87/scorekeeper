"""Celery tasks and the enqueue seam the API calls.

The pipeline orchestrator ``run_pipeline_task`` loads a persisted (already-ingested) run by id
and runs the two phases in order — **retrieval** (``evaluation.retrieve_run``: fetch/extract
each turn's source context into ``retrieved_documents``) then **evaluation**
(``evaluation.score_run``: LLM-as-a-judge scoring). Each phase owns its own database session;
the payload is just the ``run_id`` string — everything else is read from Postgres.
``score_run_task`` is kept for scoring a run on its own.

``enqueue_run`` is a thin wrapper so callers (the API, tests) enqueue through one
monkeypatchable function instead of touching the Celery task object directly.
"""

from __future__ import annotations

import logging

from scorekeeper import evaluation
from scorekeeper.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="scorekeeper.run_pipeline", acks_late=True)
def run_pipeline_task(run_id: str) -> None:
    """Run retrieval then scoring for the ingested run ``run_id`` (the worker owns sessions).

    Retrieval runs first so scoring sees the fetched/extracted context; with ``acks_late`` a
    crashed worker re-runs the whole job (retrieval is idempotent via the fetch cache).
    """
    logger.info("Tarea recibida: pipeline para run %s", run_id)
    evaluation.retrieve_run(run_id)  # phase 1: retrieval
    evaluation.score_run(run_id)  # phase 2: evaluation
    logger.info("Tarea completada: run %s", run_id)


@celery_app.task(name="scorekeeper.score_run", acks_late=True)
def score_run_task(run_id: str) -> None:
    """Score the already-ingested run ``run_id`` (the worker owns the session)."""
    logger.info("Tarea recibida: puntuar run %s", run_id)
    evaluation.score_run(run_id)
    logger.info("Tarea completada: run %s", run_id)


def enqueue_run(run_id: str) -> None:
    """Enqueue the retrieval+scoring pipeline for ``run_id`` onto the Celery queue."""
    logger.info("Encolando pipeline para run %s", run_id)
    run_pipeline_task.delay(run_id)
