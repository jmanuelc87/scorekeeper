"""Run lifecycle: read a run's summary, start it, choose which turns to score."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.services.serializers import summarize_run
from scorekeeper.core.services.status import STATUS_EN_COLA, STATUS_INGERIDO
from scorekeeper.db.connection import session_scope
from scorekeeper.db.repositories import runs as run_repo

logger = logging.getLogger(__name__)


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
        for platform_exec in run.platform_executions:
            for scenario in platform_exec.scenario_results:
                for turn in scenario.turns:
                    if turn.id in wanted:
                        turn.is_selected = is_selected
                        updated += 1
        await db.commit()
        logger.info(
            "Run %s: %d turno(s) marcados is_selected=%s.", run_id, updated, is_selected
        )
        return updated
