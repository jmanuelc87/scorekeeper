"""Celery tasks and the enqueue seam the API calls.

A run is evaluated as a **chain of one-turn jobs**, not as a single long job.
``run_chain_task`` prepares an already-ingested run (marks it in progress, pins its
prompt versions) and enqueues its first turn; ``score_turn_task`` retrieves and scores
exactly one turn and enqueues the next, waiting out the pacing delay as a Celery
``countdown`` rather than sleeping inside the job. The payloads are just an id — a
``run_id`` or a ``turn_id`` — and everything else is read from Postgres.

The point of the granularity is interruption: a worker killed mid-run loses one turn's
work instead of the run's, and the re-delivery skips every turn already done
(see :mod:`scorekeeper.core.services.chain`).

``enqueue_run`` / ``enqueue_turn`` are thin wrappers so callers (the API, the tasks
themselves, tests) enqueue through one monkeypatchable function instead of touching the
Celery task objects directly.
"""

from __future__ import annotations

import asyncio
import logging

from celery.signals import worker_process_init

from scorekeeper.core.services import chain
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


async def _start(run_id: str) -> chain.NextTurn | None:
    """Await ``start_chain``, then drain the pool before the loop closes.

    asyncpg connections are bound to the event loop that opened them, and the
    ``asyncio.run`` around each task closes its loop on return — so a pooled connection
    left behind would be unusable on the next task. Disposing here (inside the task's own
    loop, not after it) is what keeps the worker reusable.
    """
    try:
        return await chain.start_chain(run_id)
    finally:
        await engine.dispose()


async def _advance(turn_id: str) -> chain.NextTurn | None:
    """Await ``advance_chain``, disposing the pool inside the task's own loop."""
    try:
        return await chain.advance_chain(turn_id)
    finally:
        await engine.dispose()


@celery_app.task(name="scorekeeper.run_chain", acks_late=True)
def run_chain_task(run_id: str) -> None:
    """Start the chain for the ingested run ``run_id`` and enqueue its first turn.

    Celery tasks are synchronous, so the async work runs under its own event loop.
    """
    logger.info("Tarea recibida: iniciar cadena para run %s", run_id)
    nxt = asyncio.run(_start(run_id))
    if nxt is None:
        logger.info("Run %s no tiene turnos que puntuar", run_id)
        return
    enqueue_turn(nxt.turn_id, nxt.countdown)


@celery_app.task(name="scorekeeper.score_turn", acks_late=True)
def score_turn_task(turn_id: str) -> None:
    """Retrieve and score one turn, then enqueue the next turn of its run.

    The chain's own link. Enqueueing last means a worker lost after the turn was
    committed but before the hand-off is re-delivered, finds the turn already scored, and
    hands on anyway — so the chain repairs itself rather than stalling.
    """
    logger.info("Tarea recibida: puntuar turno %s", turn_id)
    nxt = asyncio.run(_advance(turn_id))
    if nxt is None:
        logger.info("Turno %s: fin de la cadena", turn_id)
        return
    enqueue_turn(nxt.turn_id, nxt.countdown)


def enqueue_run(run_id: str) -> None:
    """Enqueue the start of ``run_id``'s evaluation chain onto the Celery queue."""
    logger.info("Encolando cadena para run %s", run_id)
    run_chain_task.delay(run_id)


def enqueue_turn(turn_id: str, countdown: float) -> None:
    """Enqueue one turn, ``countdown`` seconds from now.

    The pacing between turns lives here rather than in a ``sleep`` inside the job, so a
    waiting turn occupies no worker.
    """
    logger.info("Encolando turno %s en %.2fs", turn_id, countdown)
    score_turn_task.apply_async((turn_id,), countdown=countdown)
