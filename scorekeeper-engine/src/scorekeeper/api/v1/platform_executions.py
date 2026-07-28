"""``/platform-executions`` — one flat row per platform execution, filtered."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from scorekeeper.api.v1.schemas import PlatformExecutionRead
from scorekeeper.core.services import read_models

router = APIRouter(prefix="/platform-executions", tags=["platform-executions"])


@router.get("", response_model=list[PlatformExecutionRead])
async def list_platform_executions(
    platform: str | None = Query(None, description="Coincidencia exacta de plataforma."),
    start_date: str | None = Query(
        None, description="Inicio del rango ISO-8601 sobre la ventana de evaluación."
    ),
    end_date: str | None = Query(
        None, description="Fin del rango ISO-8601 sobre la ventana de evaluación."
    ),
) -> list[dict]:
    """Retrieve the platform executions matching the filters, as a flat list.

    One entry per platform execution rather than per run, so a run holding several
    platforms yields several entries sharing a ``run_id``. Each entry carries the
    platform's rollup (``average_score``, scenario count, status breakdown) and its
    scoring window; it does not nest the scenario results — read those through
    ``GET /runs``.

    Both filters are optional and AND-combined. The date range bounds the scoring
    window (``started_at``/``finished_at``), which stays null until a worker scores
    the run, so a bound excludes queued and in-progress executions. Returns a list
    ordered by the run's creation date, then by platform; no match yields ``[]``.
    ``400`` for an unparseable date.
    """
    try:
        return await read_models.retrieve_platform_executions(
            platform=platform,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
