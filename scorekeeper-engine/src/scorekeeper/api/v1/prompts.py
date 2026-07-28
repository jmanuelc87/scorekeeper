"""``/prompts`` — the Spanish judge prompts, per metric slot, with their version history."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from scorekeeper.api.v1.schemas import PromptDetailRead, PromptRead
from scorekeeper.core.services import prompts as prompt_service

router = APIRouter(prefix="/prompts", tags=["prompts"])


# A prompt slot is code (which metric renders it, and with which variables); its text is
# user data, versioned append-only. These routes are read-only: editing and publishing
# land with the next slice. There is no DELETE and there will not be — a version a
# finished benchmark points at is what makes that benchmark's scores readable.

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
