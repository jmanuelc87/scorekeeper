"""``/runs`` — full scored details for the runs matching a set of filters."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from scorekeeper.api.v1.schemas import RunScenarioResult
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
    (``platform_executions`` → ``scenario_results`` → ``metric_scores``): the run's
    per-platform rollups, then its scenarios with their platform executions, then each
    execution's turns.
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


@router.get("/{run_id}/scenarios", response_model=list[RunScenarioResult])
async def get_run_scenarios(
    run_id: str,
    platform: str | None = Query(None, description="Coincidencia exacta de plataforma."),
    status: str | None = Query(
        None, description="Coincidencia exacta del estado del escenario."
    ),
) -> list[dict]:
    """Retrieve a run's scenario results as a flat list.

    One entry per scenario, each nesting its ``platform_executions`` — one per platform
    the scenario ran on, with that conversation's own status and average. Depth stops
    there: read a scenario's turns via ``GET /scenarios/{scenario_id}/turns`` using its
    ``id``.

    Both filters are optional, exact and AND-combined. ``platform`` selects scenarios
    that ran on it and still returns each one whole, with every platform — a scenario
    cut down to one platform is no longer a comparison. ``404`` when the ``run_id`` is
    unknown or malformed; a known run no scenario matches yields ``[]``.
    """
    scenarios = await read_models.retrieve_run_scenarios(
        run_id, platform=platform, status=status
    )
    if scenarios is None:
        raise HTTPException(status_code=404, detail=f"El run {run_id!r} no existe.")
    return scenarios
