"""The chained evaluation — one unit of work is one turn.

A run used to be a single Celery job that retrieved every turn and then scored every
turn. That job runs for as long as the run takes, so a worker killed part-way through
lost the whole delivery. Here the run is a *chain* instead: :func:`start_chain` prepares
the run and names its first turn, and each :func:`advance_chain` retrieves and scores
exactly one turn before naming the next. A killed worker therefore costs one turn, and
the pacing between turns becomes a Celery ``countdown`` rather than a ``sleep`` held
inside a job.

Both functions **return** the next unit rather than enqueueing it; :mod:`scorekeeper.tasks`
does the enqueueing. Services never import the Celery app, which is what keeps every
branch here testable without a broker and keeps the pacing policy in the domain.

The roll-ups the old nested loops did on the way out are done at the boundaries instead:
the last selected turn of a scenario rolls that scenario up, the last of a platform rolls
the platform up, and the last of the run rolls the run up. The order those boundaries
occur in is fixed by ``repositories.turns.list_selected_turn_refs``, not by relationship
ordering.

Duplicate deliveries are expected (Celery re-delivers a job whose worker died) and are
made safe two ways: a turn that already has a score is skipped rather than re-judged, and
a run-scoped advisory lock stops a duplicate chain from ever running alongside the live
one.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.config.settings import get_settings
from scorekeeper.core.metrics import selection
from scorekeeper.core.metrics.judge import Judge
from scorekeeper.core.metrics.rollup import execution_average
from scorekeeper.core.retrieval.pipeline import RetrievalOrchestrator
from scorekeeper.core.retrieval.protocols import RetrievalPipeline
from scorekeeper.core.runner import (
    MAX_TURN_ATTEMPTS,
    STATUS_FALLIDO,
    EvalRunner,
    execution_status,
    scenario_status,
    turn_delay_seconds,
)
from scorekeeper.core.services import retrieval
from scorekeeper.core.services.scoring import pin_prompts, run_status
from scorekeeper.core.services.status import STATUS_EN_PROCESO
from scorekeeper.db.connection import run_lock, session_scope
from scorekeeper.db.models import PlatformExecution, ScenarioResult, Turn, _now
from scorekeeper.db.repositories import runs as run_repo
from scorekeeper.db.repositories import turns as turn_repo

logger = logging.getLogger(__name__)


class NextTurn(NamedTuple):
    """The next unit of work, and how long to wait before starting it."""

    turn_id: str
    countdown: float


async def start_chain(
    run_id: str, *, session: AsyncSession | None = None
) -> NextTurn | None:
    """Prepare ``run_id`` for scoring and name its first turn.

    Marks the run ``en_proceso`` and pins its prompt versions — both once, for the whole
    run. The pinning stays here rather than moving into the per-turn job because it is
    also the fail-fast point: a slot with no active version must raise before a single
    judge call, not inside a worker thread where skip-metric-continue would swallow it.

    Returns ``None`` when the run has no selected turns; the run is finalized on the spot
    so it still reaches a terminal state for pollers. Raises ``ValueError`` for an unknown
    ``run_id``, and on any other failure marks the run ``fallido`` and re-raises.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run_tree(db, run_id, metric_scores=False, retrieval=False)
        if run is None:
            raise ValueError(f"El run {run_id} no existe.")

        run.status = STATUS_EN_PROCESO
        await db.commit()

        try:
            await pin_prompts(db, run)
            await db.commit()
            refs = await turn_repo.list_selected_turn_refs(db, run.id)
        except Exception:
            # rollback expires every instance, so the reload carries the loaders again.
            await db.rollback()
            await _mark_fallido(db, run_id)
            raise

        if not refs:
            logger.info("Run %s no tiene turnos seleccionados; se finaliza.", run_id)
            await _finalize_run(db, run_id)
            return None

        logger.info("Run %s: cadena iniciada sobre %d turno(s).", run_id, len(refs))
        return NextTurn(str(refs[0].turn_id), 0.0)


async def advance_chain(
    turn_id: str,
    *,
    session: AsyncSession | None = None,
    judge: Judge | None = None,
    pipeline: RetrievalPipeline | None = None,
) -> NextTurn | None:
    """Retrieve and score one turn, roll up whatever it closes, and name the next.

    The whole unit of work of a chained run. Returns the next turn to score with the
    pacing delay to wait first, or ``None`` when this turn was the last one (or when a
    duplicate delivery found the live chain already holding the run's lock).

    Raises ``ValueError`` for an unknown ``turn_id``. Any failure while working marks the
    run ``fallido`` and re-raises: the chain stops and pollers see a terminal state, the
    same contract the single-job pipeline had.
    """
    async with session_scope(session) as db:
        run_key = await turn_repo.get_run_key_for_turn(db, turn_id)
        if run_key is None:
            raise ValueError(f"El turno {turn_id} no existe.")

        # Taken before any work: a duplicate delivery must not judge the same turn as the
        # chain that is live. The duplicate that *does* get the lock (because the live
        # chain has already moved past this turn) falls through, finds the turn scored,
        # and simply hands on — which is what repairs a chain broken mid-hand-off.
        async with run_lock(db, run_key) as acquired:
            if not acquired:
                logger.info(
                    "Run %s: la cadena ya está en curso; se descarta la entrega duplicada.",
                    run_key,
                )
                return None

            turn = await turn_repo.get_turn_for_scoring(db, turn_id)
            if turn is None:
                raise ValueError(f"El turno {turn_id} no existe.")
            platform_exec = turn.platform_execution
            scenario = platform_exec.scenario_result

            refs = await turn_repo.list_selected_turn_refs(db, run_key)
            index = next(
                (i for i, ref in enumerate(refs) if ref.turn_id == turn.id), None
            )
            if index is None:  # deselected between deliveries — nothing left to do here
                logger.info("Turno %s ya no está seleccionado; se omite.", turn_id)
                return None
            nxt = refs[index + 1] if index + 1 < len(refs) else None

            # Idempotent: the first turn of the execution stamps it, later ones leave it.
            if platform_exec.started_at is None:
                platform_exec.started_at = _now()

            orchestrator = pipeline or RetrievalOrchestrator(
                session=db, encryption_key=get_settings().auth_encryption_key
            )
            try:
                await _work_turn(db, turn, scenario, orchestrator, judge)
            except Exception:
                await db.rollback()
                await _mark_fallido(db, str(run_key))
                raise

            await _close_boundaries(db, refs[index], nxt, scenario, platform_exec)
            if nxt is None:
                await _finalize_run(db, str(run_key))
                logger.info("Run %s: cadena terminada.", run_key)
                return None
            return NextTurn(str(nxt.turn_id), turn_delay_seconds())


async def _work_turn(
    db: AsyncSession,
    turn: Turn,
    scenario: ScenarioResult,
    orchestrator: RetrievalPipeline,
    judge: Judge | None,
) -> None:
    """Retrieve then score one turn, unless it is already done or out of attempts.

    Retrieval and scoring are one unit here, rather than two passes over the run, so that
    a turn is finished — or not — as a whole. The fetch cache is released at the end of
    the turn for the same reason: each job builds its own orchestrator, so nothing later
    could purge what this one downloaded.
    """
    if turn.turn_score is not None:
        # Resume-forward: an earlier delivery already scored it. No attempt is burned —
        # visiting a finished turn is not an attempt at it.
        logger.info("Turno %s ya puntuado; se omite.", turn.turn_number)
        return
    if turn.attempts >= MAX_TURN_ATTEMPTS:
        logger.warning(
            "Turno %s abandonado tras %d intento(s)", turn.turn_number, turn.attempts
        )
        return

    if not turn.retrieved_documents:
        await retrieval.retrieve_turn(turn, orchestrator)
        await db.commit()

    metric_names = await selection.metrics_for(db, scenario.use_case_id)
    templates = await selection.bound_templates(db, scenario.run_id, metric_names)
    metrics = await selection.resolve(db, scenario.use_case_id, templates)
    # run_turn counts the attempt and commits it before the first judge call.
    await EvalRunner(db, judge, templates).run_turn(
        turn, metrics, _history_before(turn.platform_execution, turn)
    )
    await _purge_turn_cache(orchestrator, turn)


async def _purge_turn_cache(pipeline: RetrievalPipeline, turn: Turn) -> None:
    """Release what this turn downloaded (best-effort).

    Per turn rather than per conversation: every job builds its own orchestrator
    and ``purge_cache`` only drops the URLs *that* instance served, so a purge deferred
    to the execution boundary would strand every earlier turn's bytes. The extracted
    markdown is already persisted, so a cleanup failure is logged, never raised.
    """
    try:
        removed = await pipeline.purge_cache()
    except Exception:  # noqa: BLE001 — cleanup must never break a scored turn
        logger.warning(
            "No se pudo limpiar la caché del turno %s", turn.turn_number, exc_info=True
        )
        return
    if removed:
        logger.info(
            "Turno %s: %d documento(s) liberado(s) de la caché", turn.turn_number, removed
        )


def _history_before(
    platform_exec: PlatformExecution, turn: Turn
) -> list[tuple[str, str]]:
    """The conversation preceding ``turn``, rebuilt from its stored execution.

    Scoped to the one platform execution, which *is* the conversation — a sibling
    platform's answers to the same scenario are a different conversation and must never
    leak into this judge's history.

    Nothing about the history travels in the message: ``platform_execution.turns`` is
    ordered by ``turn_number``, and *every* turn feeds the history whether or not it is
    selected for scoring, so the exchanges before this one are exactly the ones ahead of
    it in that list. Stops on identity rather than comparing ``turn_number``, so
    duplicate numbering cannot quietly change what the judge sees.
    """
    history: list[tuple[str, str]] = []
    for other in platform_exec.turns:
        if other.id == turn.id:
            break
        history.append((other.prompt, other.response))
    return history


async def _close_boundaries(
    db: AsyncSession,
    current: turn_repo.TurnRef,
    nxt: turn_repo.TurnRef | None,
    scenario: ScenarioResult,
    platform_exec: PlatformExecution,
) -> None:
    """Roll up whatever the turn just scored was the last of.

    A boundary is simply "the successor lives somewhere else" — or there is no
    successor. Innermost first: the platform execution closes, and if that was the
    scenario's last execution the scenario closes over it.
    """
    if nxt is not None and nxt.platform_execution_id == current.platform_execution_id:
        return
    _finalize_execution(platform_exec)
    await db.commit()
    logger.info(
        "Plataforma %s (escenario %s) finalizada: estado=%s, promedio=%s",
        platform_exec.platform,
        scenario.scenario_id,
        platform_exec.status,
        platform_exec.average_score,
    )

    if nxt is not None and nxt.scenario_result_id == current.scenario_result_id:
        return
    _finalize_scenario(scenario)
    await db.commit()
    logger.info(
        "Escenario %s finalizado: estado=%s, por plataforma=%s",
        scenario.scenario_id,
        scenario.status,
        {pe.platform: pe.average_score for pe in scenario.platform_executions},
    )


def _finalize_execution(platform_exec: PlatformExecution) -> None:
    """Write one conversation's roll-up — the tail of ``EvalRunner.run_execution``.

    Keeps that method's asymmetry on purpose: the average covers the *selected* turns,
    while the status reads ``turn_score`` across *every* turn (so an execution with a
    deselected turn reports ``parcial``). Changing it here would silently re-define what
    a stored status means.
    """
    platform_exec.average_score = execution_average(
        [turn.turn_score for turn in platform_exec.turns if turn.is_selected]
    )
    platform_exec.status = execution_status(platform_exec)
    platform_exec.finished_at = _now()


def _finalize_scenario(scenario: ScenarioResult) -> None:
    """Roll a scenario's status up over its platform executions.

    Status only: a scenario has no average, because a mean across the platforms being
    compared is not a number worth storing.
    """
    scenario.status = scenario_status(scenario)


async def _finalize_run(db: AsyncSession, run_id: str) -> None:
    """Roll the run up, sweeping the units the chain never visited.

    The chain only walks *selected* turns, so an execution with none is never reached
    and would keep its ``pending`` status for good — which ``run_status`` would then read
    as a permanently partial run. Finalizing them here is what lets a run whose selection
    covered only part of the tree still reach ``completado``.
    """
    run = await run_repo.get_run_tree(db, run_id, metric_scores=False, retrieval=False)
    if run is None:
        return
    for scenario in run.scenario_results:
        for platform_exec in scenario.platform_executions:
            if platform_exec.finished_at is None:
                platform_exec.started_at = platform_exec.started_at or _now()
                _finalize_execution(platform_exec)
        _finalize_scenario(scenario)
    run.status = run_status(run)
    await db.commit()
    logger.info("Run %s finalizado con estado %s", run_id, run.status)


async def _mark_fallido(db: AsyncSession, run_id: str) -> None:
    """Put the run in a terminal state so pollers stop waiting on a chain that stopped."""
    run = await run_repo.get_run(db, run_id)
    if run is not None:
        run.status = STATUS_FALLIDO
        await db.commit()
