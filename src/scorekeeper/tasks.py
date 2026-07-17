"""Celery tasks and the enqueue seam the API calls.

One task: ``score_run_task`` loads a persisted (already-ingested) run by id and
scores it via ``evaluation.score_run``, which owns its own database session. The
payload is just the ``run_id`` string — everything else is read from Postgres.

``enqueue_score_run`` is a thin wrapper so callers (the API, tests) enqueue through
one monkeypatchable function instead of touching the Celery task object directly.
"""

from __future__ import annotations

import logging

from scorekeeper import evaluation
from scorekeeper.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="scorekeeper.score_run", acks_late=True)
def score_run_task(run_id: str) -> None:
    """Score the already-ingested run ``run_id`` (the worker owns the session)."""
    logger.info("Tarea recibida: puntuar run %s", run_id)
    evaluation.score_run(run_id)
    logger.info("Tarea completada: run %s", run_id)


def enqueue_score_run(run_id: str) -> None:
    """Enqueue scoring for ``run_id`` onto the Celery queue."""
    logger.info("Encolando puntuación para run %s", run_id)
    score_run_task.delay(run_id)
