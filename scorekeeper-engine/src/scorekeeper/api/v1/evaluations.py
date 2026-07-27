"""``/evaluations`` — ingest uploads, start scoring, select turns, poll progress."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from scorekeeper import tasks
from scorekeeper.api.v1.schemas import (
    EvaluationEnqueuedResponse,
    EvaluationPayload,
    EvaluationResponse,
    FileOverride,
    TurnSelectionRequest,
    TurnSelectionResponse,
)
from scorekeeper.core.services import ingestion
from scorekeeper.core.services import runs as run_service
from scorekeeper.core.services import status as run_status

router = APIRouter(prefix="/evaluations", tags=["evaluations"])
logger = structlog.get_logger("scorekeeper.api")

@router.post("", response_model=EvaluationEnqueuedResponse, status_code=202)
async def create_evaluation(
    files: list[UploadFile] = File(...),
    payload: str = Form(...),
) -> EvaluationEnqueuedResponse:
    """Ingest uploaded conversation ``.xlsx`` files and persist them for scoring.

    ``multipart/form-data``: one or more ``files`` plus a ``payload`` JSON string
    (``platform``, default ``use_case``, and optional per-filename overrides). Each
    file is one scenario, scored under its own ``platform`` when the per-file override
    sets one, otherwise under the payload-level ``platform``.

    Returns ``202`` with a ``run_id`` as soon as the upload is parsed and persisted
    (status ``ingerido``). Ingestion is decoupled from scoring: nothing runs until you
    call ``POST /evaluations/{run_id}/start``, which enqueues the retrieval + LLM
    scoring pipeline. Poll ``GET /evaluations/{run_id}`` for progress and results. A
    malformed sheet or bad input is still rejected synchronously here, before anything
    is persisted.
    """
    try:
        parsed_payload = EvaluationPayload.model_validate_json(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"payload inválido: {exc}") from exc

    if not files:
        raise HTTPException(status_code=400, detail="Se requiere al menos un archivo.")

    logger.info(
        "POST /evaluations: %d archivo(s), plataforma=%s",
        len(files),
        parsed_payload.platform,
    )

    uploads: list[ingestion.UploadedFile] = []
    for upload in files:
        filename = upload.filename or "archivo.xlsx"
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(
                status_code=400, detail=f"El archivo {filename!r} no es un .xlsx."
            )
        content = await upload.read()
        if not content:
            raise HTTPException(
                status_code=400, detail=f"El archivo {filename!r} está vacío."
            )
        override = parsed_payload.files.get(filename, FileOverride())
        uploads.append(
            ingestion.UploadedFile(
                filename=filename,
                content=content,
                scenario_id=override.scenario_id or Path(filename).stem,
                use_case=override.use_case or parsed_payload.use_case,
                platform=override.platform,
            )
        )

    try:
        run_id = await ingestion.ingest_evaluation(parsed_payload.platform, uploads)
    except ingestion.UnknownUseCaseError as exc:
        # A ValueError subclass, so this arm must precede the one below.
        logger.warning("POST /evaluations rechazado: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        # Empty inputs or an unparseable sheet — a client error.
        logger.warning("POST /evaluations rechazado: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("POST /evaluations ingerido: run_id=%s", run_id)
    return EvaluationEnqueuedResponse(run_id=run_id, status=run_status.STATUS_INGERIDO)


@router.post(
    "/{run_id}/start",
    response_model=EvaluationEnqueuedResponse,
    status_code=202,
)
async def start_evaluation(run_id: str) -> EvaluationEnqueuedResponse:
    """Start scoring a previously-ingested run (the trigger decoupled from ingestion).

    Flips a run from ``ingerido`` to ``en_cola`` and enqueues the Celery pipeline
    (retrieval + LLM scoring). Returns ``202`` with ``status`` ``en_cola``; poll
    ``GET /evaluations/{run_id}`` for progress. ``404`` when the ``run_id`` is unknown,
    ``409`` when the run is not in the ``ingerido`` state (already started), so a run is
    never enqueued twice.
    """
    try:
        status = await run_service.start_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if status is None:
        raise HTTPException(status_code=404, detail=f"El run {run_id!r} no existe.")

    # enqueue_run publishes over kombu's sync SQLAlchemy transport (a blocking DB
    # round-trip), so it must not run on the event loop.
    await asyncio.to_thread(tasks.enqueue_run, run_id)
    logger.info("POST /evaluations/%s/start en cola", run_id)
    return EvaluationEnqueuedResponse(run_id=run_id, status=status)


@router.patch(
    "/{run_id}/turns/selection",
    response_model=TurnSelectionResponse,
)
async def select_turns(run_id: str, payload: TurnSelectionRequest) -> TurnSelectionResponse:
    """Mark a subset of a run's turns as selected (or not) for scoring.

    Scoring is opt-in per turn: only turns flagged ``is_selected`` are evaluated by
    the worker. Call this before ``POST /evaluations/{run_id}/start`` to pick the
    subset. Turn ids that don't belong to the run are ignored; the response reports
    how many turns were actually updated. ``404`` when the ``run_id`` is unknown,
    ``409`` when the run has already left the ``ingerido`` state (already started).
    """
    try:
        updated = await run_service.set_turn_selection(
            run_id, payload.turn_ids, payload.is_selected
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"El run {run_id!r} no existe.")

    logger.info(
        "PATCH /evaluations/%s/turns/selection: %d turno(s) is_selected=%s",
        run_id,
        updated,
        payload.is_selected,
    )
    return TurnSelectionResponse(run_id=run_id, updated=updated)


@router.get("/{run_id}", response_model=EvaluationResponse)
async def get_evaluation(run_id: str) -> EvaluationResponse:
    """Return a run's current status and summary (poll this after ``POST``).

    ``status`` walks ``ingerido → en_cola → en_recuperacion → en_proceso →
    completado|parcial|fallido``; it stays at ``ingerido`` until
    ``POST /evaluations/{run_id}/start`` enqueues it. The platform's ``average_score``
    stays ``null`` until scoring finishes. ``404`` when the ``run_id`` is unknown.
    """
    summary = await run_service.get_run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"El run {run_id!r} no existe.")
    return EvaluationResponse.model_validate(summary)
