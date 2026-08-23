"""``/scenarios/{scenario_id}/turns`` — one scenario's turns and their scores."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from scorekeeper.api.v1.schemas import ScenarioTurn
from scorekeeper.core.services import read_models

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("/{scenario_id}/turns", response_model=list[ScenarioTurn])
async def get_scenario_turns(scenario_id: str) -> list[dict]:
    """Return a scenario's turns, ordered by ``platform`` then ``turn_number``.

    ``scenario_id`` is a ``ScenarioResult`` id (its UUID) — the unique handle for one
    scenario of one run; it is surfaced as ``id`` on each scenario in ``GET /runs``. The
    non-unique human-readable ``scenario_id`` label is not accepted here.

    A scenario holds one conversation per platform, so the list spans them all and each
    turn names its ``platform`` — group by it to compare the platforms turn for turn.

    Each turn carries its content (``prompt``/``response``/``expected_output``/
    ``retrieved_context_source``), rolled-up ``turn_score`` and per-metric scores.
    ``404`` when the ``scenario_id`` is unknown or malformed.
    """
    turns = await read_models.retrieve_scenario_turns(scenario_id)
    if turns is None:
        raise HTTPException(
            status_code=404, detail=f"El escenario {scenario_id!r} no existe."
        )
    return turns
