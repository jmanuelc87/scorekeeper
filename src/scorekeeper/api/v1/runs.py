"""``/runs`` — full scored details for the runs matching a set of filters."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from scorekeeper.core.services import read_models

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
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
        return await read_models.retrieve_runs(
            run_id=run_id,
            platform=platform,
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
