"""Glue that turns uploaded conversation ``.xlsx`` files into scored results.

Ties together the two halves that already existed but were never wired: the
``importer`` (parses one ``.xlsx`` into a flat list of message dicts) and the
``EvalRunner`` (scores an already-persisted
``BenchmarkRun → PlatformExecution → ScenarioResult → Turn`` tree). In between it
*projects* parsed messages into evaluation ``Turn`` rows, builds the run hierarchy
for a **single platform** and its set of files (each file is one scenario, scored
under that one platform), seeds the metric-selection table, runs the evaluation,
and reports a JSON-serializable summary.

Ingestion and scoring are split so the start of scoring is decoupled from the upload
and can run off the request path (see ``scorekeeper.tasks``):

* :func:`ingest_evaluation` — parse the uploads and persist the run tree as
  ``ingerido`` (*persisted, not started*), returning the ``run_id``. Fast; the API
  runs it inline. It does **not** start scoring.
* :func:`start_run` — flip an ingested run from ``ingerido`` to ``en_cola`` (*queued*)
  so a caller can enqueue it. The explicit trigger the API exposes as
  ``POST /evaluations/{run_id}/start``.
* :func:`score_run` — load a queued run by id and score it with the LLM judge.
  Slow; the Celery worker runs it. The runner commits **per scenario** (its
  atomic-write unit), so an interrupted job keeps every scenario it already
  finished. On failure the run is marked ``fallido`` so pollers see a terminal state.

:func:`run_evaluation` runs ingestion and scoring on one session — the synchronous
path kept for tests and any in-process caller. :func:`get_run_summary` reads a run's
current status/summary for polling.
"""

from __future__ import annotations

import hashlib
import logging
import asyncio
import os
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.config.settings import get_settings
from scorekeeper.db.connection import session_scope
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    RetrievedContextDocument,
    ScenarioResult,
    SourceFile,
    Turn,
    TurnTokenUsage,
)
from scorekeeper.db.repositories import runs as run_repo
from scorekeeper.db.repositories import scenarios as scenario_repo
from scorekeeper.db.repositories import turns as turn_repo
from scorekeeper.importer import normalize_messages, parse_conversation
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.selection import sync_selection
from scorekeeper.retrieval.pipeline import RetrievalOrchestrator
from scorekeeper.retrieval.protocols import RetrievalPipeline
from scorekeeper.retrieval.types import STATUS_EN_RECUPERACION, RetrievalSummary
from scorekeeper.runner import (
    STATUS_COMPLETADO,
    STATUS_FALLIDO,
    STATUS_PARCIAL,
    EvalRunner,
)

logger = logging.getLogger(__name__)

DEFAULT_USE_CASE = "default"

# Orchestration-level run statuses that precede scoring (the scoring-complete
# literals — completado/parcial/fallido — live in ``runner``).
STATUS_INGERIDO = "ingerido"  # persisted, not yet started
STATUS_EN_COLA = "en_cola"  # enqueued to Celery, waiting for a worker
STATUS_EN_PROCESO = "en_proceso"  # a worker is scoring it now

# Retrieval granularity: how deep :func:`retrieve_runs` serializes the run tree.
GRANULARITY_PLATFORM = "platform_executions"  # stop at the platform execution
GRANULARITY_SCENARIO = "scenario_results"  # descend into scenario results
GRANULARITY_METRIC = "metric_scores"  # descend through turns to metric scores
_GRANULARITIES = (GRANULARITY_PLATFORM, GRANULARITY_SCENARIO, GRANULARITY_METRIC)


@dataclass
class UploadedFile:
    """One uploaded conversation file plus its resolved evaluation metadata.

    ``scenario_id`` and ``use_case`` are resolved by the caller (e.g. the HTTP
    endpoint applies per-file overrides and defaults) before reaching the
    orchestrator. ``platform`` is an optional per-file override; when ``None`` the
    file falls back to the run-level platform passed to :func:`ingest_evaluation`.

    ``messages`` carries an already-extracted conversation (the browser extension
    scrapes turns straight off a chat UI, so there is no spreadsheet to parse). When
    set, ``content`` is not parsed and only feeds the ``SourceFile`` hash — pass the
    serialized capture so the provenance hash still identifies the input.
    """

    filename: str
    content: bytes
    scenario_id: str
    use_case: str = DEFAULT_USE_CASE
    platform: str | None = None
    messages: list[dict[str, Any]] | None = None


def project_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project parsed messages into evaluation-turn dicts.

    ``parse_conversation`` yields one dict per message tagged with a ``turn``
    number (a user+model pair shares a number). This groups by that number and,
    per turn, joins the ``user`` messages into ``prompt`` and the ``model``
    messages into ``response`` (a missing side becomes ``""``), carrying through
    the raw ``retrieved_context_source`` cell / ``expected_output`` when the sheet
    provided them. Turns are returned in ascending ``turn_number`` order.
    """
    grouped: OrderedDict[int, dict[str, Any]] = OrderedDict()
    for message in messages:
        turn = message.get("turn", 1)
        bucket = grouped.setdefault(
            turn,
            {"prompt": [], "response": [], "retrieved_context_source": "", "expected_output": ""},
        )
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            bucket["prompt"].append(content)
        elif role == "model":
            bucket["response"].append(content)
        # Keep the first non-empty raw context cell and expected output seen for the turn.
        source = message.get("retrieved_context_source")
        if source and not bucket["retrieved_context_source"]:
            bucket["retrieved_context_source"] = source
        expected = message.get("expected_output")
        if expected and not bucket["expected_output"]:
            bucket["expected_output"] = expected

    turns: list[dict[str, Any]] = []
    for turn_number in sorted(grouped):
        bucket = grouped[turn_number]
        turns.append(
            {
                "turn_number": turn_number,
                "prompt": "\n".join(bucket["prompt"]),
                "response": "\n".join(bucket["response"]),
                "retrieved_context_source": bucket["retrieved_context_source"] or None,
                "expected_output": bucket["expected_output"] or None,
            }
        )
    return turns


async def ingest_evaluation(
    platform: str,
    files: list[UploadedFile],
    *,
    session: AsyncSession | None = None,
) -> str:
    """Parse ``files`` and persist a queued run for ``platform``; return its ``run_id``.

    Builds one ``BenchmarkRun`` and, under it, one ``PlatformExecution`` per distinct
    platform — each file lands under ``upload.platform`` when set, otherwise under the
    run-level ``platform`` fallback. Each file becomes one ``ScenarioResult`` (with its
    ``Turn`` rows) attached to its platform's execution. The run is committed with
    status ``ingerido`` (persisted, not yet started) and left unscored; a caller later
    starts it via :func:`start_run` (which enqueues :func:`score_run`).

    This is the fast, on-request half: parsing is synchronous so a malformed sheet is
    rejected here (as ``ValueError``) before anything is persisted. ``session`` defaults
    to ``SessionLocal()``; tests inject an in-memory session.

    Raises ``ValueError`` when ``platform`` or ``files`` is empty, or when a file
    cannot be parsed (propagated from ``parse_conversation``); the transaction is
    rolled back first so nothing is persisted.
    """
    if not platform:
        raise ValueError("Se requiere una plataforma.")
    if not files:
        raise ValueError("Se requiere al menos un archivo.")

    async with session_scope(session) as db:
        try:
            # Seed the use_case -> metric table so resolve_scenario finds metrics;
            # without this every turn scores None and every scenario is "fallido".
            await sync_selection(db)
            await db.flush()

            # Parse each file once and record its provenance as a SourceFile.
            parsed: list[tuple[UploadedFile, list[dict[str, Any]], SourceFile]] = []
            for upload in files:
                messages = await _parse_upload(upload)
                logger.info(
                    "Analizado %s: %d mensaje(s) -> %d turno(s)",
                    upload.filename,
                    len(messages),
                    len(project_turns(messages)),
                )
                source_file = SourceFile(
                    filename=upload.filename,
                    file_hash=hashlib.sha256(upload.content).hexdigest(),
                )
                db.add(source_file)
                parsed.append((upload, messages, source_file))

            run = BenchmarkRun(status=STATUS_INGERIDO)
            # A run references a single source file; only meaningful with one upload.
            if len(parsed) == 1:
                run.source_file = parsed[0][2]

            # One PlatformExecution per distinct resolved platform (first-seen order);
            # each file's optional override wins over the run-level fallback.
            executions: dict[str, PlatformExecution] = {}
            for upload, messages, _ in parsed:
                resolved = upload.platform or platform
                platform_exec = executions.get(resolved)
                if platform_exec is None:
                    platform_exec = PlatformExecution(platform=resolved, run=run)
                    executions[resolved] = platform_exec
                scenario = ScenarioResult(
                    scenario_id=upload.scenario_id,
                    use_case=upload.use_case,
                    source_ref=upload.filename,
                    raw_conversation={"messages": messages},
                    platform_execution=platform_exec,
                )
                for turn in project_turns(messages):
                    # Store the raw context cell; the retrieval pipeline (score-time worker)
                    # fetches/extracts it into ``retrieved_documents`` — nothing is decoupled here.
                    turn_row = Turn(
                        turn_number=turn["turn_number"],
                        prompt=turn["prompt"],
                        response=turn["response"],
                        expected_output=turn["expected_output"],
                        retrieved_context_source=turn["retrieved_context_source"],
                    )
                    scenario.turns.append(turn_row)

            db.add(run)
            # Counted from the parsed input rather than by walking the committed run tree:
            # the relationships were built in memory here, but re-reading them after the
            # commit would be a lazy load the async session cannot serve.
            turn_total = sum(len(project_turns(messages)) for _, messages, _ in parsed)
            await db.commit()
            logger.info(
                "Run %s ingerido: %d plataforma(s) × %d archivo(s) = %d turno(s).",
                run.id,
                len(executions),
                len(parsed),
                turn_total,
            )
            return str(run.id)
        except Exception:
            await db.rollback()
            raise


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
            await EvalRunner(db, judge).run_benchmark(run)  # scores everything, commits
        except Exception:
            # rollback expires every instance, so the reload carries the loaders again.
            await db.rollback()
            run = await run_repo.get_run_tree(db, run_id)
            if run is not None:
                run.status = STATUS_FALLIDO
                await db.commit()
            raise

        run.status = _run_status(run)
        await db.commit()
        logger.info("Run %s finalizado con estado %s", run.id, run.status)
        return _summarize(run)


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

    When a platform execution's turns are all retrieved, its downloaded documents are purged
    from the fetch cache (:func:`_purge_cache`) — the extracted markdown is persisted by then,
    so the bytes are dead weight. The failure path purges too, leaving no orphaned downloads.

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
            for platform_exec in run.platform_executions:
                for scenario in platform_exec.scenario_results:
                    for turn in scenario.turns:
                        # Skip retrieval for turns that won't be scored; their
                        # retrieved context is only used by their own metrics.
                        if turn.is_selected:
                            await _retrieve_turn(turn, orchestrator)
                    await db.commit()  # atomic-write unit: one scenario at a time
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
        return _summarize(run)


async def _purge_cache(
    pipeline: RetrievalPipeline, platform_exec: PlatformExecution | None = None
) -> None:
    """Release the documents downloaded for one platform execution (best-effort).

    Retrieval is the only phase that needs the fetched bytes — scoring reads the extracted
    markdown off ``retrieved_documents`` — so once a platform execution's turns are done the
    cache entries it created are dropped from disk and from ``document_cache``. A cleanup
    failure is logged and swallowed: the context is already persisted, so a stranded blob is
    not worth failing the run over.
    """
    label = platform_exec.platform if platform_exec is not None else "?"
    try:
        removed = await pipeline.purge_cache()
    except Exception:  # noqa: BLE001 — cleanup must never break a completed retrieval
        logger.warning("No se pudo limpiar la caché de %s", label, exc_info=True)
        return
    logger.info("Plataforma %s: %d documento(s) liberado(s) de la caché", label, removed)


async def _retrieve_turn(turn: Turn, pipeline: RetrievalPipeline) -> None:
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
        run_id = await ingest_evaluation(platform, files, session=db)
        await _select_all_turns(db, run_id)
        await retrieve_run(run_id, session=db, pipeline=pipeline)
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


async def get_run_summary(
    run_id: str, *, session: AsyncSession | None = None
) -> dict[str, Any] | None:
    """Return a run's current status/summary for polling, or ``None`` if unknown."""
    async with session_scope(session) as db:
        run = await run_repo.get_run_tree(db, run_id, metric_scores=False, retrieval=False)
        return _summarize(run) if run is not None else None


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


async def retrieve_turn_traces(
    turn_id: str,
    *,
    include_provenance: bool = True,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]] | None:
    """Structured metric traces for one turn, or ``None`` if the turn is unknown.

    One entry per metric scored on the turn (in metric-score order): its
    ``metric_name`` and ``trace`` (``{"steps": [...]}`` or ``None``). With
    ``include_provenance`` each entry also carries ``judge_model`` and
    ``rubric_version``. A malformed or unknown ``turn_id`` yields ``None`` (the HTTP
    layer maps that to ``404``); a turn with no scores yields ``[]``.
    """
    async with session_scope(session) as db:
        turn = await turn_repo.get_turn_with_traces(db, turn_id)
        if turn is None:
            return None
        return [
            _serialize_metric_trace(score, include_provenance)
            for score in turn.metric_scores
        ]


async def retrieve_turn_token_usage(
    turn_id: str,
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """LLM token usage for scoring one turn, or ``None`` if the turn is unknown.

    Returns the turn's 1:1 :class:`TurnTokenUsage` as
    ``{"turn_id", "input_tokens", "output_tokens", "total_tokens"}`` (``total_tokens``
    is the derived ``input + output``, never stored). This is a raw per-turn read — no
    aggregation across turns, scenarios or platforms. A turn that was never scored (no
    usage row) reports zeros. A malformed or unknown ``turn_id`` yields ``None`` (the
    HTTP layer maps that to ``404``).
    """
    async with session_scope(session) as db:
        turn = await turn_repo.get_turn_with_token_usage(db, turn_id)
        if turn is None:
            return None
        return _serialize_turn_token_usage(turn)


async def retrieve_scenario_turns(
    scenario_id: str,
    *,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]] | None:
    """Return one scenario's turns in ``turn_number`` order, or ``None`` if unknown.

    ``scenario_id`` is a ``ScenarioResult`` id (its UUID) — the unique handle for one
    conversation scored under one platform in one run; the human-readable
    ``ScenarioResult.scenario_id`` label is *not* unique and is not accepted here.
    Discover the UUID from ``GET /runs`` (each scenario carries its ``id``).

    Each entry carries the turn's content (``prompt``/``response``/``expected_output``/
    ``retrieved_context_source``), its rolled-up ``turn_score``, and per-metric scores
    (without the structured ``trace`` — read that via :func:`retrieve_turn_traces`). A
    malformed or unknown ``scenario_id`` yields ``None`` (the HTTP layer maps that to
    ``404``); a scenario with no turns yields ``[]``.
    """
    async with session_scope(session) as db:
        scenario = await scenario_repo.get_scenario_with_turns(db, scenario_id)
        if scenario is None:
            return None
        return [_serialize_scenario_turn(turn) for turn in scenario.turns]


async def retrieve_runs(
    *,
    run_id: str | None = None,
    platform: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = GRANULARITY_SCENARIO,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Return full scored details for the runs matching the given filters.

    Every filter is optional and combined with AND:

    * ``run_id`` — narrow to a single run. An unknown/invalid id yields ``[]``.
    * ``platform`` — exact, case-sensitive match on ``PlatformExecution.platform``
      (e.g. ``"claude"``, ``"copilot"``, ``"gemini"``).
    * ``start_date`` / ``end_date`` — an ISO-8601 range (``YYYY-MM-DD`` or a full
      timestamp) over the **scoring window**: ``started_at >= start_date`` and
      ``finished_at <= end_date``. Those columns stay ``NULL`` until a worker scores
      the run, so a bound naturally excludes queued/in-progress runs.

    ``granularity`` controls how deep each run is serialized:
    ``platform_executions`` → ``scenario_results`` → ``metric_scores`` (see
    :func:`_serialize_run`). Metric scores never carry their structured ``trace``;
    it is persisted for direct inspection but not surfaced here. Raises
    ``ValueError`` for an unknown granularity or an unparseable date. Results are
    ordered by ``BenchmarkRun.created_at``.
    """
    if granularity not in _GRANULARITIES:
        raise ValueError(
            f"Granularidad {granularity!r} inválida; use una de {_GRANULARITIES}."
        )
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    key: uuid.UUID | None = None
    if run_id is not None:
        try:
            key = uuid.UUID(run_id)
        except ValueError:
            return []

    async with session_scope(session) as db:
        runs = await run_repo.list_runs(
            db,
            run_key=key,
            platform=platform,
            start=start,
            end=end,
            with_metric_scores=granularity == GRANULARITY_METRIC,
        )
        return [_serialize_run(run, granularity) for run in runs]


def _parse_date(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 ``value`` (date or timestamp), or ``None`` when unset."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} {value!r} inválido; use un formato ISO-8601 (YYYY-MM-DD)."
        ) from exc


async def _parse_upload(upload: UploadedFile) -> list[dict[str, Any]]:
    """Write ``upload`` to a temp ``.xlsx`` and parse it into raw messages.

    ``parse_conversation`` needs a filesystem path (openpyxl opens by path), so the
    in-memory upload is spilled to a short-lived temp file that is always removed.
    An upload that already carries ``messages`` (a browser capture) skips the
    spreadsheet entirely and is only normalized.

    The spreadsheet path is blocking file + CPU work and this now runs on the event
    loop (the API's routes are coroutines), so it is offloaded to a worker thread.
    """
    if upload.messages is not None:
        messages = normalize_messages(upload.messages)
        # Expose the captured context under the same ``retrieved_context_source`` key
        # the .xlsx path uses (see ``parse_conversation``), so ``_group_turns`` stores
        # it on the turn and the retrieval pipeline can interpret it later.
        for message in messages:
            if "retrieved_context" in message:
                message["retrieved_context_source"] = message.pop("retrieved_context")
        return messages

    return await asyncio.to_thread(_parse_xlsx_bytes, upload.content)


def _parse_xlsx_bytes(content: bytes) -> list[dict[str, Any]]:
    """Spill ``content`` to a temp ``.xlsx`` and parse it (blocking; run off-loop)."""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        return parse_conversation(path)
    finally:
        os.unlink(path)


def _run_status(run: BenchmarkRun) -> str:
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


#: Statuses at which scoring has stopped — nothing more will be scored.
_TERMINAL_STATUSES = {STATUS_COMPLETADO, STATUS_PARCIAL, STATUS_FALLIDO}


def _run_progress(run: BenchmarkRun) -> dict[str, Any]:
    """Turn-level progress of a run: how many turns have been scored so far.

    A turn counts as done once ``run_turn`` sets its ``turn_score``. While the run is
    still queued/running the ratio climbs live; once it reaches a terminal status it
    is pinned to 1.0 — a turn whose every metric failed keeps ``turn_score = None``
    (skip-metric-continue), so a finished run must not read < 100% forever.
    """
    turns = [
        turn
        for platform_exec in run.platform_executions
        for scenario in platform_exec.scenario_results
        for turn in scenario.turns
    ]
    total = len(turns)
    if run.status in _TERMINAL_STATUSES:
        done = total
    else:
        done = sum(turn.turn_score is not None for turn in turns)
    return {"done": done, "total": total, "ratio": round(done / total, 4) if total else 0.0}


def _summarize(run: BenchmarkRun) -> dict[str, Any]:
    """Project the scored run into the JSON-serializable API response shape.

    A run may hold one ``PlatformExecution`` per distinct platform (files can carry
    a per-file platform override), so ``platforms`` is a list.
    """
    return {
        "run_id": str(run.id),
        "status": run.status,
        "progress": _run_progress(run),
        "platforms": [
            _platform_summary(platform_exec)
            for platform_exec in run.platform_executions
        ],
    }


def _platform_summary(platform_exec: PlatformExecution) -> dict[str, Any]:
    """One platform execution's rollup: average, scenario count, status breakdown."""
    breakdown: dict[str, int] = {}
    for scenario in platform_exec.scenario_results:
        breakdown[scenario.status] = breakdown.get(scenario.status, 0) + 1
    return {
        "platform": platform_exec.platform,
        "average_score": platform_exec.average_score,
        "scenarios": len(platform_exec.scenario_results),
        "status_breakdown": breakdown,
    }


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 string for a timestamp column, or ``None`` when unset."""
    return value.isoformat() if value is not None else None


def _serialize_run(run: BenchmarkRun, granularity: str) -> dict[str, Any]:
    """Project a run into a JSON-serializable dict, deepened to ``granularity``.

    ``platform_executions`` emits run + per-platform rollups; ``scenario_results``
    adds each scenario; ``metric_scores`` adds each turn and its metric scores. All
    rollup values are already-persisted columns — nothing is recomputed here.
    """
    return {
        "run_id": str(run.id),
        "status": run.status,
        "created_at": _iso(run.created_at),
        "progress": _run_progress(run),
        "platforms": [
            _serialize_platform(platform_exec, granularity)
            for platform_exec in run.platform_executions
        ],
    }


def _serialize_platform(
    platform_exec: PlatformExecution, granularity: str
) -> dict[str, Any]:
    breakdown: dict[str, int] = {}
    for scenario in platform_exec.scenario_results:
        breakdown[scenario.status] = breakdown.get(scenario.status, 0) + 1
    entry: dict[str, Any] = {
        "platform": platform_exec.platform,
        "average_score": platform_exec.average_score,
        "started_at": _iso(platform_exec.started_at),
        "finished_at": _iso(platform_exec.finished_at),
        "scenarios": len(platform_exec.scenario_results),
        "status_breakdown": breakdown,
    }
    if granularity in (GRANULARITY_SCENARIO, GRANULARITY_METRIC):
        entry["scenario_results"] = [
            _serialize_scenario(scenario, granularity)
            for scenario in platform_exec.scenario_results
        ]
    return entry


def _serialize_scenario(
    scenario: ScenarioResult, granularity: str
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        # The row's UUID — the handle GET /scenarios/{id}/turns takes (distinct from the
        # human-readable, non-unique ``scenario_id`` label below).
        "id": str(scenario.id),
        "scenario_id": scenario.scenario_id,
        "use_case": scenario.use_case,
        "status": scenario.status,
        "average_score": scenario.average_score,
    }
    if granularity == GRANULARITY_METRIC:
        entry["turns"] = [_serialize_turn(turn) for turn in scenario.turns]
    return entry


def _serialize_metric_trace(
    score: MetricScore, include_provenance: bool
) -> dict[str, Any]:
    """One metric's trace for the per-turn traces endpoint (steps, optional provenance)."""
    entry: dict[str, Any] = {"metric_name": score.metric_name}
    if include_provenance:
        entry["judge_model"] = score.judge_model
        entry["rubric_version"] = score.rubric_version
    entry["trace"] = {"steps": score.trace.steps} if score.trace is not None else None
    return entry


def _serialize_turn_token_usage(turn: Turn) -> dict[str, Any]:
    """A turn's raw token usage; zeros when the turn has no usage row yet.

    ``total_tokens`` is derived (``input + output``), matching ``TurnTokenUsage`` which
    never stores the total.
    """
    usage = turn.token_usage or TurnTokenUsage(input_tokens=0, output_tokens=0)
    return {
        "turn_id": str(turn.id),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }


def _serialize_scenario_turn(turn: Turn) -> dict[str, Any]:
    """Full turn view for the per-scenario turns endpoint: content + scores.

    Unlike :func:`_serialize_turn` (the ``/runs`` metric-granularity projection, which
    carries only ids/scores), this surfaces the conversation content — ``prompt``,
    ``response``, ``expected_output`` and the raw ``retrieved_context_source`` — so a
    caller can read the scenario's turns without re-uploading the source. The
    structured metric ``trace`` is still not surfaced here (read it via
    ``/turns/{turn_id}/traces``).
    """
    return {
        "turn_id": str(turn.id),
        "turn_number": turn.turn_number,
        "prompt": turn.prompt,
        "response": turn.response,
        "expected_output": turn.expected_output,
        "retrieved_context_source": turn.retrieved_context_source,
        "turn_score": turn.turn_score,
        "metric_scores": [
            {
                "metric_name": score.metric_name,
                "score": score.score,
                "judge_model": score.judge_model,
                "rubric_version": score.rubric_version,
            }
            for score in turn.metric_scores
        ],
    }


def _serialize_turn(turn: Turn) -> dict[str, Any]:
    # The structured ``trace`` is intentionally not surfaced here: it is persisted
    # on the ``metric_traces`` table for direct inspection, but the read surface
    # (HTTP ``/runs``) does not expose it.
    return {
        "turn_id": str(turn.id),
        "turn_number": turn.turn_number,
        "turn_score": turn.turn_score,
        "metric_scores": [
            {
                "metric_name": score.metric_name,
                "score": score.score,
                "judge_model": score.judge_model,
                "rubric_version": score.rubric_version,
            }
            for score in turn.metric_scores
        ],
    }
