"""``/captures`` — the JSON twin of ``/evaluations`` for the browser extension."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, HTTPException

from scorekeeper.api.v1.schemas import CapturePayload, EvaluationEnqueuedResponse
from scorekeeper.core.services import ingestion
from scorekeeper.core.services import status as run_status

router = APIRouter(prefix="/captures", tags=["captures"])
logger = structlog.get_logger("scorekeeper.api")

@router.post("", response_model=EvaluationEnqueuedResponse, status_code=202)
async def create_capture(payload: CapturePayload) -> EvaluationEnqueuedResponse:
    """Ingest conversations captured from a chat UI and persist them for scoring.

    The JSON twin of ``POST /evaluations`` for clients that already hold the turns
    and have no spreadsheet to upload — the browser extension in ``extension/``
    scrapes them off Copilot, Gemini and Claude. Each entry in ``conversations`` is
    one scenario and follows the same platform rules as an uploaded file: its own
    ``platform`` when set, otherwise the payload-level one.

    Returns ``202`` with a ``run_id`` at status ``ingerido``. Like ``POST
    /evaluations``, ingestion is decoupled from scoring: call ``POST
    /evaluations/{run_id}/start`` to enqueue the pipeline, then poll
    ``GET /evaluations/{run_id}`` for progress and results.
    """
    logger.info(
        "POST /captures: %d conversación(es), plataforma=%s",
        len(payload.conversations),
        payload.platform,
    )

    uploads: list[ingestion.UploadedFile] = []
    for conversation in payload.conversations:
        messages = [
            message.model_dump(exclude_none=True) for message in conversation.messages
        ]
        if not any(message.get("content", "").strip() for message in messages):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"La conversación {conversation.scenario_id!r} no tiene "
                    "mensajes con contenido."
                ),
            )
        source_ref = conversation.source_ref or conversation.scenario_id
        uploads.append(
            ingestion.UploadedFile(
                filename=source_ref,
                # No file bytes to hash, so the capture itself is the provenance.
                content=json.dumps(messages, ensure_ascii=False).encode("utf-8"),
                scenario_id=conversation.scenario_id,
                use_case=conversation.use_case or payload.use_case,
                platform=conversation.platform,
                messages=messages,
                # A cleared field in the extension's popup arrives as "", which means
                # "unknown" exactly like an omitted key — store one value for both.
                model_name=(conversation.model_name or "").strip() or None,
            )
        )

    try:
        run_id = await ingestion.ingest_evaluation(payload.platform, uploads)
    except ValueError as exc:
        logger.warning("POST /captures rechazado: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("POST /captures ingerido: run_id=%s", run_id)
    return EvaluationEnqueuedResponse(run_id=run_id, status=run_status.STATUS_INGERIDO)
