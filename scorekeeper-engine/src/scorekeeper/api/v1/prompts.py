"""``/prompts`` — the Spanish judge prompts, per metric slot, with their version history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from scorekeeper.api.v1.schemas import (
    PromptDetailRead,
    PromptRead,
    PromptVersionCreate,
    PromptVersionPublish,
    PromptVersionRead,
)
from scorekeeper.core.services import prompts as prompt_service

router = APIRouter(prefix="/prompts", tags=["prompts"])


# A prompt slot is code (which metric renders it, and with which variables); its text is
# user data, versioned append-only. The writes are named transitions rather than a
# generic PATCH, because each flips rows *other* than the one addressed and carries its
# own gate: draft → publish (validates, and activates, deactivating the incumbent) →
# activate (rollback to an earlier published version) / discard (drafts only).
#
# There is no DELETE and there will not be — a version a finished benchmark points at is
# what makes that benchmark's scores readable. There is no deactivate either: is_active
# only ever moves, so a slot that has a live version keeps one.

@router.get("", response_model=list[PromptRead])
async def list_prompts() -> list[dict[str, Any]]:
    """List every prompt slot with its active version, ordered by metric then slug.

    Reconciles the catalog against the code registry first, so a slot declared by a
    metric but never yet synced appears here with its seeded v1.
    """
    return await prompt_service.list_prompts()


@router.get("/{prompt_id}", response_model=PromptDetailRead)
async def get_prompt(prompt_id: str) -> dict[str, Any]:
    """One prompt slot with its full edit history, newest version first.

    ``404`` when the id is unknown or malformed.
    """
    prompt = await prompt_service.get_prompt(prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"El prompt {prompt_id} no existe.")
    return prompt


def _version_missing(prompt_id: str, version_id: str) -> HTTPException:
    """The 404 the three transitions share, covering an unknown id of either kind.

    A version belonging to a *different* prompt lands here too: the service looks it up
    within the addressed slot's own history, so it is unknown by construction.
    """
    return HTTPException(
        status_code=404,
        detail=f"La versión {version_id} del prompt {prompt_id} no existe.",
    )


@router.post("/{prompt_id}/versions", response_model=PromptVersionRead, status_code=201)
async def create_prompt_version(
    prompt_id: str, body: PromptVersionCreate
) -> dict[str, Any]:
    """Open a new draft of a prompt slot's text.

    An open draft is discarded and replaced: ``template`` is write-once even as a draft,
    so every save is a new row and version numbers have gaps. The template is validated
    at publish, not here. ``404`` when the prompt id is unknown or malformed, ``422``
    when the template is blank.
    """
    try:
        version = await prompt_service.create_version(
            prompt_id,
            body.template,
            changelog=body.changelog,
            author=body.author,
        )
    except prompt_service.PromptValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if version is None:
        raise HTTPException(status_code=404, detail=f"El prompt {prompt_id} no existe.")
    return version


@router.post(
    "/{prompt_id}/versions/{version_id}/publish", response_model=PromptVersionRead
)
async def publish_prompt_version(
    prompt_id: str, version_id: str, body: PromptVersionPublish
) -> dict[str, Any]:
    """Validate a draft against its slot's contract and make it the live version.

    Publishing activates: the previously active version is deactivated in the same
    transaction. ``404`` unknown; ``409`` when the version is not a draft; ``422`` when
    the template misses a required variable or uses one the slot does not declare.
    """
    try:
        version = await prompt_service.publish_version(
            prompt_id, version_id, author=body.author
        )
    except prompt_service.PromptValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except prompt_service.PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if version is None:
        raise _version_missing(prompt_id, version_id)
    return version


@router.post(
    "/{prompt_id}/versions/{version_id}/activate", response_model=PromptVersionRead
)
async def activate_prompt_version(prompt_id: str, version_id: str) -> dict[str, Any]:
    """Roll the live version back to an already-published one.

    Idempotent: activating the version that is already live returns it unchanged.
    ``404`` unknown; ``409`` for a draft or a discarded version.
    """
    try:
        version = await prompt_service.activate_version(prompt_id, version_id)
    except prompt_service.PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if version is None:
        raise _version_missing(prompt_id, version_id)
    return version


@router.post(
    "/{prompt_id}/versions/{version_id}/discard", response_model=PromptVersionRead
)
async def discard_prompt_version(prompt_id: str, version_id: str) -> dict[str, Any]:
    """Abandon an open draft. The row and its version number are kept.

    ``404`` unknown; ``409`` for anything that is not a draft — a published version is
    never discardable, even when inactive, because a finished run may bind it.
    """
    try:
        version = await prompt_service.discard_version(prompt_id, version_id)
    except prompt_service.PromptConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if version is None:
        raise _version_missing(prompt_id, version_id)
    return version
