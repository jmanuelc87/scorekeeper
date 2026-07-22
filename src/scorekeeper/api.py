import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from scorekeeper import evaluation, tasks
from scorekeeper.config import get_settings
from scorekeeper.retrieval.credentials import service as auth_providers
from scorekeeper.retrieval.credentials.service import (
    ProviderConflictError,
    ProviderValidationError,
)

# Surface app (INFO) logs in the container output; uvicorn only configures its own
# loggers, so without this our progress logs would be swallowed.
logging.basicConfig(level=logging.INFO)
# httpx logs every judge request at INFO, which floods the output; quiet it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("scorekeeper.api")

settings = get_settings()

# The database schema is owned by Alembic — run `alembic upgrade head`
# before starting the app (the compose `migrate` service does this).
app = FastAPI(title="Scorekeeper Results API", version="0.1.0")
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
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluations", response_model=EvaluationEnqueuedResponse, status_code=202)
def create_evaluation(
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
        content = upload.file.read()
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
        run_id = evaluation.ingest_evaluation(parsed_payload.platform, uploads)
    except ValueError as exc:
        # Empty inputs or an unparseable sheet — a client error.
        logger.warning("POST /evaluations rechazado: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tasks.enqueue_run(run_id)
    logger.info("POST /evaluations en cola: run_id=%s", run_id)
    return EvaluationEnqueuedResponse(run_id=run_id, status=evaluation.STATUS_EN_COLA)


@app.get("/evaluations/{run_id}", response_model=EvaluationResponse)
def get_evaluation(run_id: str) -> EvaluationResponse:
    """Return a run's current status and summary (poll this after ``POST``).

    ``status`` walks ``en_cola → en_proceso → completado|parcial|fallido``; the
    platform's ``average_score`` stays ``null`` until scoring finishes. ``404`` when
    the ``run_id`` is unknown.
    """
    summary = evaluation.get_run_summary(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"El run {run_id!r} no existe.")
    return EvaluationResponse.model_validate(summary)


@app.get("/runs")
def list_runs(
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

    The HTTP twin of the MCP ``retrieve`` tool. All filters are optional and
    AND-combined; ``granularity`` controls depth (``platform_executions`` →
    ``scenario_results`` → ``metric_scores``). Returns a list ordered by creation date;
    an unknown ``run_id`` yields ``[]``. ``400`` for an unknown ``granularity`` or an
    unparseable date.
    """
    try:
        return evaluation.retrieve_runs(
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
def list_auth_providers(
    provider: str | None = Query(None, description="Filtra por tipo de proveedor."),
    host: str | None = Query(None, description="Coincidencia exacta de host."),
    enabled: bool | None = Query(None, description="Filtra por estado habilitado."),
) -> list[dict[str, Any]]:
    """List configured credential providers (filters optional, AND-combined)."""
    return auth_providers.list_providers(provider=provider, host=host, enabled=enabled)


@app.post("/auth-providers", response_model=AuthProviderRead, status_code=201)
def create_auth_provider(body: AuthProviderCreate) -> dict[str, Any]:
    """Create a credential provider row. ``409`` on a duplicate ``(provider, host)``."""
    try:
        return auth_providers.create_provider(body.model_dump())
    except ProviderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/auth-providers/{provider_id}", response_model=AuthProviderRead)
def get_auth_provider(provider_id: UUID) -> dict[str, Any]:
    """Return one credential provider. ``404`` when the id is unknown."""
    row = auth_providers.get_provider(provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
    return row


@app.patch("/auth-providers/{provider_id}", response_model=AuthProviderRead)
def update_auth_provider(provider_id: UUID, body: AuthProviderUpdate) -> dict[str, Any]:
    """Partially update a credential provider. ``404`` unknown; ``409`` on a duplicate key."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No hay campos para actualizar.")
    try:
        row = auth_providers.update_provider(provider_id, changes)
    except ProviderValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")
    return row


@app.delete("/auth-providers/{provider_id}", status_code=204)
def delete_auth_provider(provider_id: UUID) -> None:
    """Delete a credential provider. ``404`` when the id is unknown."""
    if not auth_providers.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail=f"El proveedor {provider_id} no existe.")


def main() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
