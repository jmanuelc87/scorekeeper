"""The retrieval phase — resolve each selected turn's cited context.

Walks the run tree and runs the retrieval pipeline
(:mod:`scorekeeper.core.retrieval`) for every selected turn, replacing that
turn's stored documents with what the pipeline resolved. Best-effort per turn:
the pipeline maps its own stage failures to a status rather than raising.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.pipeline import RetrievalOrchestrator
from scorekeeper.core.retrieval.protocols import RetrievalPipeline
from scorekeeper.core.retrieval.types import STATUS_EN_RECUPERACION, RetrievalSummary
from scorekeeper.core.runner import MAX_TURN_ATTEMPTS, STATUS_FALLIDO
from scorekeeper.core.services.serializers import summarize_run
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import PlatformExecution, RetrievedContextDocument, Turn
from scorekeeper.db.repositories import runs as run_repo

logger = logging.getLogger(__name__)


async def retrieve_run(
    run_id: str,
    *,
    session: AsyncSession | None = None,
    pipeline: RetrievalPipeline | None = None,
) -> dict[str, Any]:
    """Run the retrieval pipeline over a queued run, populating each turn's context.

    Loads the run, marks it ``en_recuperacion``, and for each turn parses/fetches/extracts its
    raw ``retrieved_context_source`` cell into ``retrieved_documents`` — best-effort, so a
    per-document failure is recorded in the pipeline report (and dropped from the context)
    rather than raised. Commits **per scenario** so an interrupted job keeps finished scenarios;
    a hard phase failure marks the run ``fallido`` and re-raises.

    Resumes forward: a turn that already has ``retrieved_documents`` was resolved by an
    earlier delivery and is skipped, so a re-delivery costs at most the turn that was in
    flight rather than re-fetching the whole run. Each started turn counts an attempt and
    is abandoned past ``MAX_TURN_ATTEMPTS`` (see :mod:`scorekeeper.core.runner`).

    When one conversation's turns are all retrieved, its downloaded documents are purged
    from the fetch cache (:func:`_purge_cache`) — the extracted markdown is persisted by
    then, so the bytes are dead weight. The failure path purges too, leaving no orphaned
    downloads.

    ``session`` defaults to ``SessionLocal()``; ``pipeline`` to a ``RetrievalOrchestrator`` bound
    to that session (tests inject a fake). This is the retrieval half the worker runs before
    scoring. Returns the run summary.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run_tree(db, run_id, metric_scores=False)
        if run is None:
            raise ValueError(f"El run {run_id} no existe.")

        run.status = STATUS_EN_RECUPERACION
        await db.commit()

        orchestrator = pipeline or RetrievalOrchestrator(
            session=db, encryption_key=get_settings().auth_encryption_key
        )
        logger.info("Recuperando contexto para run %s…", run.id)
        try:
            for scenario in run.scenario_results:
                for platform_exec in scenario.platform_executions:
                    for turn in platform_exec.turns:
                        # Skip retrieval for turns that won't be scored (their retrieved
                        # context is only used by their own metrics) and for turns an
                        # earlier delivery already resolved — that is what makes a
                        # re-delivery resume forward instead of re-fetching everything.
                        if not turn.is_selected or turn.retrieved_documents:
                            continue
                        if turn.attempts >= MAX_TURN_ATTEMPTS:
                            logger.warning(
                                "Turno %s abandonado tras %d intento(s)",
                                turn.turn_number,
                                turn.attempts,
                            )
                            continue
                        # Commit before fetching: the count only bounds retries if it
                        # survives the crash that caused the retry.
                        turn.attempts += 1
                        await db.commit()
                        await retrieve_turn(turn, orchestrator)
                    await db.commit()  # atomic-write unit: one conversation at a time
                    await _purge_cache(orchestrator, platform_exec)
        except Exception:
            # rollback expires every instance, so the reload carries the loaders again.
            await db.rollback()
            run = await run_repo.get_run_tree(db, run_id, metric_scores=False)
            if run is not None:
                run.status = STATUS_FALLIDO
                await db.commit()
            # a failed phase leaves no downloads behind either
            await _purge_cache(orchestrator)
            raise

        logger.info("Recuperación completada para run %s", run.id)
        return summarize_run(run)


async def _purge_cache(
    pipeline: RetrievalPipeline, platform_exec: PlatformExecution | None = None
) -> None:
    """Release the documents downloaded for one conversation (best-effort).

    Retrieval is the only phase that needs the fetched bytes — scoring reads the extracted
    markdown off ``retrieved_documents`` — so once a conversation's turns are done the
    cache entries it created are dropped from disk and from ``document_cache``. A cleanup
    failure is logged and swallowed: the context is already persisted, so a stranded blob
    is not worth failing the run over.
    """
    label = platform_exec.platform if platform_exec is not None else "?"
    try:
        removed = await pipeline.purge_cache()
    except Exception:  # noqa: BLE001 — cleanup must never break a completed retrieval
        logger.warning("No se pudo limpiar la caché de %s", label, exc_info=True)
        return
    logger.info("Plataforma %s: %d documento(s) liberado(s) de la caché", label, removed)


async def retrieve_turn(turn: Turn, pipeline: RetrievalPipeline) -> None:
    """Populate one turn's ``retrieved_documents`` from its raw context cell (best-effort)."""
    turn.retrieved_documents.clear()  # idempotent when a run is retried
    cell = (turn.retrieved_context_source or "").strip()
    if not cell:
        return
    report = await pipeline.run(cell)
    for rank, document in enumerate(report.to_context().documents):
        turn.retrieved_documents.append(RetrievedContextDocument.from_document(document, rank))
    summary = RetrievalSummary.from_outcomes(report.outcomes)
    logger.info(
        "Turno %s: %d/%d documento(s) recuperado(s)",
        turn.turn_number,
        summary.retrieved,
        summary.total,
    )
