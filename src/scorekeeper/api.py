import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from scorekeeper import evaluation, tasks
from scorekeeper.config import get_settings
from scorekeeper.database import engine
from scorekeeper.logging_config import configure_logging
from scorekeeper.retrieval.credentials import service as auth_providers
from scorekeeper.retrieval.credentials.service import (
    ProviderConflictError,
    ProviderValidationError,
)

settings = get_settings()

# Plain-text logging to stdout. uvicorn only configures its own loggers, so
# without this our app events would be swallowed.
configure_logging(settings.log_level)
logger = structlog.get_logger("scorekeeper.api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Drain the asyncpg connection pool on shutdown."""
    yield
    await engine.dispose()


# The database schema is owned by Alembic — run `alembic upgrade head`
# before starting the app (the compose `migrate` service does this).
app = FastAPI(title="Scorekeeper Results API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


class FileOverride(BaseModel):
    """Per-file metadata overriding the request defaults, keyed by filename."""

    scenario_id: str | None = None
    use_case: str | None = None
    # Overrides the payload-level platform for just this file; None uses the default.
    platform: str | None = None


class EvaluationPayload(BaseModel):
    """The JSON metadata part of a ``POST /evaluations`` multipart request."""

    # Default platform for the upload; a file may override it via its FileOverride.
    platform: str = Field(..., min_length=1)
    use_case: str = evaluation.DEFAULT_USE_CASE
    # Optional per-filename overrides; a file with no entry uses the defaults.
    files: dict[str, FileOverride] = Field(default_factory=dict)


class CaptureMessage(BaseModel):
    """One scraped chat bubble: who said it and what it said."""

    role: str = Field(..., min_length=1)  # user | model (aliases are normalized)
    content: str = ""
    # Explicit turn number; omitted for every message means turns are derived
    # (each user message following a non-user message opens a new turn).
    turn: int | None = None
    retrieved_context: str | None = None
    expected_output: str | None = None


class CaptureConversation(BaseModel):
    """One captured conversation — the JSON twin of one uploaded ``.xlsx`` file."""

    scenario_id: str = Field(..., min_length=1)
    messages: list[CaptureMessage] = Field(..., min_length=1)
    # Overrides the payload-level defaults for just this conversation.
    use_case: str | None = None
    platform: str | None = None
    # Where the capture came from (e.g. the chat URL); stored as the source ref.
    source_ref: str | None = None


class CapturePayload(BaseModel):
    """Body of ``POST /captures``: conversations scraped from a chat UI."""

    platform: str = Field(..., min_length=1)
    use_case: str = evaluation.DEFAULT_USE_CASE
    conversations: list[CaptureConversation] = Field(..., min_length=1)


class PlatformSummary(BaseModel):
    platform: str
    average_score: float | None
    scenarios: int
    status_breakdown: dict[str, int]


class RunProgress(BaseModel):
    """Turn-level progress: ``done`` of ``total`` turns scored (``ratio`` 0.0–1.0)."""

    done: int
    total: int
    ratio: float


class EvaluationResponse(BaseModel):
    run_id: str
    status: str
    progress: RunProgress
    # One entry per distinct platform in the run (files may override the platform).
    platforms: list[PlatformSummary]


class EvaluationEnqueuedResponse(BaseModel):
    """Returned by ``POST /evaluations``: the run was queued for a worker to score."""

    run_id: str
    status: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluations", response_model=EvaluationEnqueuedResponse, status_code=202)
async def create_evaluation(
    files: list[UploadFile] = File(...),
    payload: str = Form(...),
) -> EvaluationEnqueuedResponse:
    """Ingest uploaded conversation ``.xlsx`` files and enqueue them for scoring.

    ``multipart/form-data``: one or more ``files`` plus a ``payload`` JSON string
    (``platform``, default ``use_case``, and optional per-filename overrides). Each
    file is one scenario, scored under its own ``platform`` when the per-file override
    sets one, otherwise under the payload-level ``platform``.

    Returns ``202`` with a ``run_id`` as soon as the upload is parsed and persisted
    (status ``en_cola``); a Celery worker then runs the retrieval pipeline and the LLM
    scoring off the request path. Poll ``GET /evaluations/{run_id}`` for progress and
    results. A malformed sheet or bad input is still rejected synchronously here, before
    anything queues.
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

    uploads: list[evaluation.UploadedFile] = []
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
            evaluation.UploadedFile(
                filename=filename,
                content=content,
                scenario_id=override.scenario_id or Path(filename).stem,
                use_case=override.use_case or parsed_payload.use_case,
                platform=override.platform,
            )
        )

    try:
        run_id = await evaluation.ingest_evaluation(parsed_payload.platform, uploads)
    except ValueError as exc:
        # Empty inputs or an unparseable sheet — a client error.
        logger.warning("POST /evaluations rechazado: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # enqueue_run publishes over kombu's sync SQLAlchemy transport (a blocking DB
    # round-trip), so it must not run on the event loop.
    await asyncio.to_thread(tasks.enqueue_run, run_id)
    logger.info("POST /evaluations en cola: run_id=%s", run_id)
    return EvaluationEnqueuedResponse(run_id=run_id, status=evaluation.STATUS_EN_COLA)


@app.post("/captures", response_model=EvaluationEnqueuedResponse, status_code=202)
async def create_capture(payload: CapturePayload) -> EvaluationEnqueuedResponse:
    """Ingest conversations captured from a chat UI and enqueue them for scoring.

    The JSON twin of ``POST /evaluations`` for clients that already hold the turns
    and have no spreadsheet to upload — the browser extension in ``extension/``
    scrapes them off Copilot, Gemini and Claude. Each entry in ``conversations`` is
    one scenario and follows the same platform rules as an uploaded file: its own
    ``platform`` when set, otherwise the payload-level one.

    Returns ``202`` with a ``run_id``; poll ``GET /evaluations/{run_id}`` for
    progress and results, exactly as with an upload.
    """
    logger.info(
        "POST /captures: %d conversación(es), plataforma=%s",
        len(payload.conversations),
        payload.platform,
    )

    uploads: list[evaluation.UploadedFile] = []
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
            evaluation.UploadedFile(
                filename=source_ref,
                # No file bytes to hash, so the capture itself is the provenance.
                content=json.dumps(messages, ensure_ascii=False).encode("utf-8"),
                scenario_id=conversation.scenario_id,
                use_case=conversation.use_case or payload.use_case,
                platform=conversation.platform,
                messages=messages,
            )
        )

    try:
        run_id = await evaluation.ingest_evaluation(payload.platform, uploads)
    except ValueError as exc:
        logger.warning("POST /captures rechazado: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # enqueue_run publishes over kombu's sync SQLAlchemy transport (a blocking DB
    # round-trip), so it must not run on the event loop.
    await asyncio.to_thread(tasks.enqueue_run, run_id)
    logger.info("POST /captures en cola: run_id=%s", run_id)
    return EvaluationEnqueuedResponse(run_id=run_id, status=evaluation.STATUS_EN_COLA)


@app.get("/evaluations/{run_id}", response_model=EvaluationResponse)
async def get_evaluation(run_id: str) -> EvaluationResponse:
    """Return a run's current status and summary (poll this after ``POST``).

    ``status`` walks ``en_cola → en_proceso → completado|parcial|fallido``; the
    platform's ``average_score`` stays ``null`` until scoring finishes. ``404`` when
    the ``run_id`` is unknown.
    """
    summary = await evaluation.get_run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"El run {run_id!r} no existe.")
    return EvaluationResponse.model_validate(summary)


class ScenarioTurnMetric(BaseModel):
    """One metric's score on a turn (without its structured trace)."""

    metric_name: str
    score: float
    judge_model: str | None = None
    rubric_version: str | None = None


class ScenarioTurn(BaseModel):
    """One turn of a scenario: its conversation content plus per-metric scores."""

    turn_id: str
    turn_number: int
    prompt: str
    response: str
    expected_output: str | None = None
    retrieved_context_source: str | None = None
    turn_score: float | None = None
    metric_scores: list[ScenarioTurnMetric]


@app.get("/scenarios/{scenario_id}/turns", response_model=list[ScenarioTurn])
async def get_scenario_turns(scenario_id: str) -> list[dict]:
    """Return a scenario's turns, in ``turn_number`` order.

    ``scenario_id`` is a ``ScenarioResult`` id (its UUID) — the unique handle for one
    conversation scored under one platform in one run; it is surfaced as ``id`` on each
    scenario in ``GET /runs``. The non-unique human-readable ``scenario_id`` label is
    not accepted here.

    Each turn carries its content (``prompt``/``response``/``expected_output``/
    ``retrieved_context_source``), rolled-up ``turn_score`` and per-metric scores.
    ``404`` when the ``scenario_id`` is unknown or malformed.
    """
    turns = await evaluation.retrieve_scenario_turns(scenario_id)
    if turns is None:
        raise HTTPException(
            status_code=404, detail=f"El escenario {scenario_id!r} no existe."
        )
    return turns


@app.get("/turns/{turn_id}/traces")
async def get_turn_traces(
    turn_id: str,
    provenance: bool = Query(
        True, description="Incluir judge_model y rubric_version por métrica."
    ),
) -> list[dict]:
    """Return the structured metric traces for one turn.

    One entry per metric scored on the turn: its ``metric_name`` and ``trace``
    (``{"steps": [...]}`` or ``null``). ``provenance=true`` (default) also includes
    ``judge_model`` and ``rubric_version``; ``provenance=false`` returns the minimal
    shape. ``404`` when the ``turn_id`` is unknown or malformed.
    """
    traces = await evaluation.retrieve_turn_traces(turn_id, include_provenance=provenance)
    if traces is None:
        raise HTTPException(status_code=404, detail=f"El turno {turn_id!r} no existe.")
    return traces


class TurnTokenUsage(BaseModel):
    """A turn's raw LLM token usage; ``total_tokens`` is the derived ``input + output``."""

    turn_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


@app.get("/turns/{turn_id}/token-usage", response_model=TurnTokenUsage)
async def get_turn_token_usage(turn_id: str) -> dict:
    """Return the LLM token usage for scoring one turn (no aggregation).

    The turn's 1:1 ``TurnTokenUsage``: ``input_tokens``, ``output_tokens`` and the
    derived ``total_tokens``. A turn that was never scored reports zeros. ``404`` when
    the ``turn_id`` is unknown or malformed.
    """
    usage = await evaluation.retrieve_turn_token_usage(turn_id)
    if usage is None:
        raise HTTPException(status_code=404, detail=f"El turno {turn_id!r} no existe.")
    return usage


@app.get("/runs")
async def list_runs(
    run_id: str | None = Query(None, description="Limita a una sola evaluación."),
    platform: str | None = Query(None, description="Coincidencia exacta de plataforma."),
    start_date: str | None = Query(
        None, description="Inicio del rango ISO-8601 sobre la ventana de evaluación."
    ),
    end_date: str | None = Query(
        None, description="Fin del rango ISO-8601 sobre la ventana de evaluación."
    ),
    granularity: str = Query(
        "scenario_results",
        description="platform_executions | scenario_results | metric_scores",
    ),
) -> list[dict]:
    """Retrieve full scored details for the runs matching the filters.

    All filters are optional and AND-combined; ``granularity`` controls depth
    (``platform_executions`` → ``scenario_results`` → ``metric_scores``).
    Returns a list ordered by creation date;
    an unknown ``run_id`` yields ``[]``. ``400`` for an unknown ``granularity`` or an
    unparseable date.

    Metric scores are returned without their structured ``trace``, which is
    persisted for direct inspection but not surfaced through this API.
    """
    try:
        return await evaluation.retrieve_runs(
            run_id=run_id,
            platform=platform,
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Auth provider CRUD ---------------------------------------------------------------
#
# Manage the retrieval pipeline's credential store (the ``auth_providers`` table). The
# certificate ``private_key`` is **write-only**: it is accepted on create/update, stored
# encrypted, and never returned — reads expose only ``has_private_key``. These endpoints
# manage secrets and carry no built-in auth, so restrict them at the network/deployment layer.


class AuthProviderCreate(BaseModel):
    """Body for ``POST /auth-providers``. ``private_key`` is a PEM, stored encrypted."""

    provider: str = Field(..., min_length=1, description="Provider kind, e.g. 'sharepoint'.")
    host: str = Field(..., min_length=1, description="Gated host this row authorizes.")
    enabled: bool = True
    tenant_id: str | None = None
    client_id: str | None = None
    thumbprint: str | None = None
    site_url: str | None = None
    settings: dict[str, Any] | None = None
    # Write-only certificate private key (PEM); encrypted at rest, never returned.
    private_key: str | None = None


class AuthProviderUpdate(BaseModel):
    """Body for ``PATCH /auth-providers/{id}``. Only the fields present are changed.

    Sending ``private_key`` rotates the stored key (a falsy value clears it); omitting it
    leaves the key untouched.
    """

    provider: str | None = Field(None, min_length=1)
    host: str | None = Field(None, min_length=1)
    enabled: bool | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    thumbprint: str | None = None
    site_url: str | None = None
    settings: dict[str, Any] | None = None
    private_key: str | None = None


class AuthProviderRead(BaseModel):
    """Safe read view of an ``auth_providers`` row — no secret material."""

    id: str
    provider: str
    host: str
    enabled: bool
    tenant_id: str | None
    client_id: str | None
    thumbprint: str | None
    site_url: str | None
    # Whether an (encrypted) certificate private key is stored; the key itself is never emitted.
    has_private_key: bool
    settings: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@app.get("/auth-providers", response_model=list[AuthProviderRead])
async def list_auth_providers(
    provider: str | None = Query(None, description="Filtra por tipo de proveedor."),
    host: str | None = Query(None, description="Coincidencia exacta de host."),
    enabled: bool | None = Query(None, description="Filtra por estado habilitado."),
) -> list[dict[str, Any]]:
    """List configured credential providers (filters optional, AND-combined)."""
    return await auth_providers.list_providers(provider=provider, host=host, enabled=enabled)


@app.post("/auth-providers", response_model=AuthProviderRead, status_code=201)
async def create_auth_provider(body: AuthProviderCreate) -> dict[str, Any]:
    """Create a credential provider row. ``409`` on a duplicate ``(provider, host)``."""
    try:
        return await auth_providers.create_provider(body.model_dump())
    except ProviderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/auth-providers/{provider_id}", response_model=AuthProviderRead)
async def get_auth_provider(provider_id: UUID) -> dict[str, Any]:
    """Return one credential provider. ``404`` when the id is unknown."""
    row = await auth_providers.get_provider(provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
    return row


@app.patch("/auth-providers/{provider_id}", response_model=AuthProviderRead)
async def update_auth_provider(provider_id: UUID, body: AuthProviderUpdate) -> dict[str, Any]:
    """Partially update a credential provider. ``404`` unknown; ``409`` on a duplicate key."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No hay campos para actualizar.")
    try:
        row = await auth_providers.update_provider(provider_id, changes)
    except ProviderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
    return row


@app.delete("/auth-providers/{provider_id}", status_code=204)
async def delete_auth_provider(provider_id: UUID) -> None:
    """Delete a credential provider. ``404`` when the id is unknown."""
    if not await auth_providers.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")


def main() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
