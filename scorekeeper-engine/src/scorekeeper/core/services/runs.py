"""Run lifecycle: read a run's summary, start it, choose which turns to score."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.runner import STATUS_COMPLETADO, STATUS_FALLIDO, STATUS_PARCIAL
from scorekeeper.core.services.serializers import summarize_run
from scorekeeper.core.services.status import STATUS_EN_COLA, STATUS_INGERIDO
from scorekeeper.db.connection import session_scope
from scorekeeper.db.repositories import prompts as prompt_repo
from scorekeeper.db.repositories import runs as run_repo

logger = logging.getLogger(__name__)

# The states a run may be resumed from: it stopped without scoring everything it had
# selected. ``fallido`` is a chain that died on an error; ``parcial`` is one that
# finished walking but left turns unscored. ``en_cola``/``en_proceso`` are excluded
# because the chain is still live — Celery re-delivers a job whose worker died, so a
# run in those states repairs itself rather than needing a second chain.
RESUMABLE_STATUSES = frozenset({STATUS_FALLIDO, STATUS_PARCIAL})


async def get_run_summary(
    run_id: str, *, session: AsyncSession | None = None
) -> dict[str, Any] | None:
    """Return a run's current status/summary for polling, or ``None`` if unknown."""
    async with session_scope(session) as db:
        run = await run_repo.get_run_tree(db, run_id, metric_scores=False, retrieval=False)
        return summarize_run(run) if run is not None else None


async def start_run(run_id: str, *, session: AsyncSession | None = None) -> str | None:
    """Move an ingested run to ``en_cola`` so it can be enqueued for scoring.

    Decouples the start of evaluation from ingestion: :func:`ingest_evaluation` leaves
    the run at ``ingerido`` and a separate call flips it to ``en_cola``, after which the
    caller enqueues the Celery pipeline. Commits the new status; the caller enqueues.

    Returns the new status (``en_cola``) on success, or ``None`` when the ``run_id`` is
    unknown/invalid. Raises ``ValueError`` when the run is not in the ``ingerido`` state
    (already started), so a run is never enqueued twice.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run(db, run_id)
        if run is None:
            return None
        if run.status != STATUS_INGERIDO:
            raise ValueError(
                f"El run {run_id} no está en estado '{STATUS_INGERIDO}' "
                f"(estado actual: '{run.status}')."
            )
        run.status = STATUS_EN_COLA
        await db.commit()
        logger.info("Run %s encolado para puntuación.", run_id)
        return STATUS_EN_COLA


async def resume_run(run_id: str, *, session: AsyncSession | None = None) -> str | None:
    """Move a stopped run back to ``en_cola`` so its chain can be enqueued again.

    Resuming re-enqueues the *same* chain: :func:`~scorekeeper.core.services.chain.start_chain`
    re-pins the prompts and walks the run's selected turns from the start, and each turn
    that already has a score is skipped rather than re-judged, so only the unscored ones
    cost a judge call. Commits the new status; the caller enqueues.

    Returns the new status (``en_cola``) on success, or ``None`` when the ``run_id`` is
    unknown/invalid. Raises ``ValueError`` when the run is not in a resumable state (see
    :data:`RESUMABLE_STATUSES`) — a run still queued or in process has a live chain, and
    one already ``completado`` has nothing left to score.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run(db, run_id)
        if run is None:
            return None
        if run.status not in RESUMABLE_STATUSES:
            raise ValueError(
                f"El run {run_id} no se puede reanudar desde el estado "
                f"'{run.status}'. Estados reanudables: "
                f"{', '.join(sorted(RESUMABLE_STATUSES))}."
            )
        run.status = STATUS_EN_COLA
        await db.commit()
        logger.info("Run %s reanudado: encolado de nuevo para puntuación.", run_id)
        return STATUS_EN_COLA


async def rerun_run(run_id: str, *, session: AsyncSession | None = None) -> str | None:
    """Score a finished run again from scratch: drop its scores and re-enqueue it.

    The counterpart of :func:`resume_run`, and deliberately not the same call. A resume
    walks forward over the turns still unscored, so on a ``completado`` run it would
    enqueue a chain that skips every turn and does nothing. A re-run instead clears the
    scores (see :func:`~scorekeeper.db.repositories.runs.clear_scores`) so every selected
    turn is judged again, and drops the run's prompt bindings so
    :func:`~scorekeeper.core.services.scoring.pin_prompts` re-pins the *active* versions
    — which is the point of asking for one: the same conversations under an updated
    rubric. Turn selection and retrieved context survive, so the re-run pays for judge
    calls only. Commits the new status; the caller enqueues.

    Returns the new status (``en_cola``) on success, or ``None`` when the ``run_id`` is
    unknown/invalid. Raises ``ValueError`` when the run is not ``completado`` — a run
    that stopped early is a resume, not a re-run, and re-running it would throw away the
    scores it did produce.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run(db, run_id)
        if run is None:
            return None
        if run.status != STATUS_COMPLETADO:
            raise ValueError(
                f"El run {run_id} no se puede re-ejecutar desde el estado "
                f"'{run.status}'. Solo un run '{STATUS_COMPLETADO}' se re-ejecuta."
            )
        run_key = run.id
        run.status = STATUS_EN_COLA
        # Flushed before the wipe, and the key read before it: ``clear_scores`` expires
        # the session, which would drop a status still pending in memory and turn a
        # later ``run.id`` into a lazy refresh (a ``MissingGreenlet`` under asyncio).
        await db.flush()
        await run_repo.clear_scores(db, run_key)
        await prompt_repo.replace_run_bindings(db, run_key, [])
        await db.commit()
        logger.info("Run %s re-ejecutado: puntuaciones borradas y encolado.", run_id)
        return STATUS_EN_COLA


async def set_turn_selection(
    run_id: str,
    turn_ids: list[str],
    is_selected: bool,
    *,
    session: AsyncSession | None = None,
) -> int | None:
    """Flag the given turns of a run as selected/deselected for scoring.

    Only selected turns are evaluated by the worker (retrieval + LLM judge); this
    is how a caller picks the subset to score before starting the run. Returns the
    number of turns updated, or ``None`` when the ``run_id`` is unknown/invalid.
    Raises ``ValueError`` when the run has already left the ``ingerido`` state
    (selection must happen before starting). Turn ids that don't belong to the run
    (or are malformed) are ignored.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run_tree(db, run_id, metric_scores=False, retrieval=False)
        if run is None:
            return None
        if run.status != STATUS_INGERIDO:
            raise ValueError(
                f"El run {run_id} no está en estado '{STATUS_INGERIDO}' "
                f"(estado actual: '{run.status}')."
            )
        wanted: set[uuid.UUID] = set()
        for raw in turn_ids:
            try:
                wanted.add(uuid.UUID(raw))
            except (ValueError, AttributeError):
                continue  # ignore malformed ids
        updated = 0
        for scenario in run.scenario_results:
            for platform_exec in scenario.platform_executions:
                for turn in platform_exec.turns:
                    if turn.id in wanted:
                        turn.is_selected = is_selected
                        updated += 1
        await db.commit()
        logger.info(
            "Run %s: %d turno(s) marcados is_selected=%s.", run_id, updated, is_selected
        )
        return updated
