"""Request and response models for the v1 API.

One module rather than one per route: ``EvaluationEnqueuedResponse`` is returned
by four routes across two modules, so per-module schemas would force route
modules to import each other — the coupling the split exists to remove.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from scorekeeper.core.services import ingestion


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
    use_case: str = ingestion.DEFAULT_USE_CASE
    # Optional per-filename overrides; a file with no entry uses the defaults.
    files: dict[str, FileOverride] = Field(default_factory=dict)


class EvaluationEnqueuedResponse(BaseModel):
    """The ``run_id`` and ``status`` returned by the ingest and start endpoints.

    ``POST /evaluations`` and ``POST /captures`` persist the run and return it at
    ``ingerido`` (not yet started); ``POST /evaluations/{run_id}/start`` flips it to
    ``en_cola`` and enqueues a worker.
    """

    run_id: str
    status: str


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
    # The model that produced the responses, when the client detected it. Stored on
    # the scenario; None (or blank) means the client could not tell.
    model_name: str | None = None
    # Where the capture came from (e.g. the chat URL); stored as the source ref.
    source_ref: str | None = None


class CapturePayload(BaseModel):
    """Body of ``POST /captures``: conversations scraped from a chat UI."""

    platform: str = Field(..., min_length=1)
    use_case: str = ingestion.DEFAULT_USE_CASE
    conversations: list[CaptureConversation] = Field(..., min_length=1)


class TurnSelectionRequest(BaseModel):
    """Which turns of a run to (de)select for scoring, sent to the selection endpoint."""

    turn_ids: list[str]
    is_selected: bool = True


class TurnSelectionResponse(BaseModel):
    """How many turns the selection endpoint updated."""

    run_id: str
    updated: int


class RunProgress(BaseModel):
    """Turn-level progress: ``done`` of ``total`` turns scored (``ratio`` 0.0–1.0)."""

    done: int
    total: int
    ratio: float


class PlatformSummary(BaseModel):
    platform: str
    average_score: float | None
    scenarios: int
    status_breakdown: dict[str, int]


class EvaluationResponse(BaseModel):
    run_id: str
    status: str
    progress: RunProgress
    # One entry per distinct platform in the run (files may override the platform).
    platforms: list[PlatformSummary]


class ScenarioTurnMetric(BaseModel):
    """One metric's score on a turn (without its structured trace).

    ``score`` is ``null`` when the metric did not apply to the turn — it had
    nothing to measure (no retrieved context, no claims), so it is also left out
    of the turn, scenario and platform averages.
    """

    metric_name: str
    score: float | None = None
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


class TurnTokenUsage(BaseModel):
    """A turn's raw LLM token usage; ``total_tokens`` is the derived ``input + output``."""

    turn_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


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


class UseCaseCreate(BaseModel):
    """Body for ``POST /use-cases`` — a name and the metrics it scores."""

    name: str = Field(..., min_length=1, description="Nombre único del caso de uso.")
    metrics: list[str] = Field(
        ..., min_length=1, description="Nombres de métricas, tal como los lista GET /metrics."
    )


class UseCaseRead(BaseModel):
    """A use case and the metric names linked to it."""

    id: str
    name: str
    metrics: list[str]


class MetricRead(BaseModel):
    """One registered metric — the catalog a use case is composed from."""

    name: str
    category: str
    weight: float
    rubric_version: str


class PromptVersionRead(BaseModel):
    """One edit of one prompt slot.

    ``template`` is write-once, so a version is a permanent record of the text a
    benchmark scored under. ``published_by``/``published_at`` are null while the
    version is a draft or has been discarded.
    """

    id: str
    version: int
    template: str
    status: str
    is_active: bool
    changelog: str | None
    created_by: str | None
    created_at: datetime
    published_by: str | None
    published_at: datetime | None


class PromptRead(BaseModel):
    """One prompt slot a metric renders, with the version runs currently bind."""

    id: str
    metric: str
    slug: str
    # The placeholders the metric fills itself; a template must use exactly these,
    # plus optionally {prompt}, {response} and {context}, which the judge fills.
    required_variables: list[str]
    description: str
    # Null when no published version is active for this slot.
    active_version: PromptVersionRead | None


class PromptDetailRead(BaseModel):
    """One prompt slot with its full edit history, newest version first.

    Version numbers have gaps: the counter is assigned at row creation, so a discarded
    draft keeps its number and never appears as published.
    """

    id: str
    metric: str
    slug: str
    required_variables: list[str]
    description: str
    versions: list[PromptVersionRead]


class PromptVersionCreate(BaseModel):
    """Body for ``POST /prompts/{prompt_id}/versions`` — a new draft of a slot's text.

    ``template`` is not validated against the slot's contract here; that gate is publish.
    A draft you cannot save until it is correct is not a draft.
    """

    template: str = Field(
        ...,
        min_length=1,
        description="Texto de la plantilla; no se puede modificar después de crearla.",
    )
    changelog: str | None = Field(None, description="Por qué se hizo la edición.")
    # ``max_length`` matches ``String(128)`` on ``created_by``: without it an
    # over-long name is a database error rather than a 422.
    author: str | None = Field(
        None, max_length=128, description="Quién escribe el borrador."
    )


class PromptVersionPublish(BaseModel):
    """Body for ``POST /prompts/{prompt_id}/versions/{version_id}/publish``."""

    author: str | None = Field(
        None, max_length=128, description="Quién publica la versión."
    )
