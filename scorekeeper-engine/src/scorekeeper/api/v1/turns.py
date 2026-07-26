"""``/turns/{turn_id}`` — a turn's metric traces and its judge token usage."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from scorekeeper.api.v1.schemas import TurnTokenUsage
from scorekeeper.core.services import read_models

router = APIRouter(prefix="/turns", tags=["turns"])


@router.get("/{turn_id}/traces")
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
    traces = await read_models.retrieve_turn_traces(turn_id, include_provenance=provenance)
    if traces is None:
        raise HTTPException(status_code=404, detail=f"El turno {turn_id!r} no existe.")
    return traces


@router.get("/{turn_id}/token-usage", response_model=TurnTokenUsage)
async def get_turn_token_usage(turn_id: str) -> dict:
    """Return the LLM token usage for scoring one turn (no aggregation).

    The turn's 1:1 ``TurnTokenUsage``: ``input_tokens``, ``output_tokens`` and the
    derived ``total_tokens``. A turn that was never scored reports zeros. ``404`` when
    the ``turn_id`` is unknown or malformed.
    """
    usage = await read_models.retrieve_turn_token_usage(turn_id)
    if usage is None:
        raise HTTPException(status_code=404, detail=f"El turno {turn_id!r} no existe.")
    return usage
