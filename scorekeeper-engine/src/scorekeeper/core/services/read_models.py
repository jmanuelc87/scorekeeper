"""Query-side services: the reads behind the results endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.services.serializers import (
    GRANULARITY_METRIC,
    GRANULARITY_SCENARIO,
    _GRANULARITIES,
    serialize_metric_trace,
    serialize_platform_execution,
    serialize_run,
    serialize_run_scenario,
    serialize_scenario_turn,
    serialize_turn_token_usage,
)
from scorekeeper.db.connection import session_scope
from scorekeeper.db.repositories import platform_executions as platform_execution_repo
from scorekeeper.db.repositories import runs as run_repo
from scorekeeper.db.repositories import scenarios as scenario_repo
from scorekeeper.db.repositories import turns as turn_repo


async def retrieve_turn_traces(
    turn_id: str,
    *,
    include_provenance: bool = True,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]] | None:
    """Structured metric traces for one turn, or ``None`` if the turn is unknown.

    One entry per metric scored on the turn (in metric-score order): its
    ``metric_name`` and ``trace`` (``{"steps": [...]}`` or ``None``). With
    ``include_provenance`` each entry also carries ``judge_model`` and
    ``rubric_version``. A malformed or unknown ``turn_id`` yields ``None`` (the HTTP
    layer maps that to ``404``); a turn with no scores yields ``[]``.
    """
    async with session_scope(session) as db:
        turn = await turn_repo.get_turn_with_traces(db, turn_id)
        if turn is None:
            return None
        return [
            serialize_metric_trace(score, include_provenance)
            for score in turn.metric_scores
        ]


async def retrieve_turn_token_usage(
    turn_id: str,
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """LLM token usage for scoring one turn, or ``None`` if the turn is unknown.

    Returns the turn's 1:1 :class:`TurnTokenUsage` as
    ``{"turn_id", "input_tokens", "output_tokens", "total_tokens"}`` (``total_tokens``
    is the derived ``input + output``, never stored). This is a raw per-turn read — no
    aggregation across turns, scenarios or platforms. A turn that was never scored (no
    usage row) reports zeros. A malformed or unknown ``turn_id`` yields ``None`` (the
    HTTP layer maps that to ``404``).
    """
    async with session_scope(session) as db:
        turn = await turn_repo.get_turn_with_token_usage(db, turn_id)
        if turn is None:
            return None
        return serialize_turn_token_usage(turn)


async def retrieve_scenario_turns(
    scenario_id: str,
    *,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]] | None:
    """Return one scenario's turns in ``turn_number`` order, or ``None`` if unknown.

    ``scenario_id`` is a ``ScenarioResult`` id (its UUID) — the unique handle for one
    conversation scored under one platform in one run; the human-readable
    ``ScenarioResult.scenario_id`` label is *not* unique and is not accepted here.
    Discover the UUID from ``GET /runs`` (each scenario carries its ``id``).

    Each entry carries the turn's content (``prompt``/``response``/``expected_output``/
    ``retrieved_context_source``), its rolled-up ``turn_score``, and per-metric scores
    (without the structured ``trace`` — read that via :func:`retrieve_turn_traces`). A
    malformed or unknown ``scenario_id`` yields ``None`` (the HTTP layer maps that to
    ``404``); a scenario with no turns yields ``[]``.
    """
    async with session_scope(session) as db:
        scenario = await scenario_repo.get_scenario_with_turns(db, scenario_id)
        if scenario is None:
            return None
        return [serialize_scenario_turn(turn) for turn in scenario.turns]


async def retrieve_run_scenarios(
    run_id: str,
    *,
    platform: str | None = None,
    status: str | None = None,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]] | None:
    """Return one run's scenario results as a flat list, or ``None`` if unknown.

    ``run_id`` is a ``BenchmarkRun`` id (its UUID). Each entry is a scenario rollup —
    ``id``, ``scenario_id``, ``platform``, ``use_case``, ``model_name``, ``status`` and
    ``average_score`` — flattened across the run's platform executions, so a run holding
    several platforms yields every scenario in one list. Turns are not included; read
    them via :func:`retrieve_scenario_turns` using each entry's ``id``.

    Both filters are optional, exact and combined with AND: ``platform`` matches
    ``PlatformExecution.platform`` (case-sensitive), ``status`` matches
    ``ScenarioResult.status``.

    A malformed or unknown ``run_id`` yields ``None`` (the HTTP layer maps that to
    ``404``); a known run whose scenarios no filter matches yields ``[]``. The run is
    looked up first precisely so those two cases stay distinguishable.
    """
    async with session_scope(session) as db:
        run = await run_repo.get_run(db, run_id)
        if run is None:
            return None
        scenarios = await scenario_repo.list_scenarios_for_run(
            db, run.id, platform=platform, status=status
        )
        return [serialize_run_scenario(scenario) for scenario in scenarios]


async def retrieve_runs(
    *,
    run_id: str | None = None,
    platform: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = GRANULARITY_SCENARIO,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Return full scored details for the runs matching the given filters.

    Every filter is optional and combined with AND:

    * ``run_id`` — narrow to a single run. An unknown/invalid id yields ``[]``.
    * ``platform`` — exact, case-sensitive match on ``PlatformExecution.platform``
      (e.g. ``"claude"``, ``"copilot"``, ``"gemini"``).
    * ``start_date`` / ``end_date`` — an ISO-8601 range (``YYYY-MM-DD`` or a full
      timestamp) over the **scoring window**: ``started_at >= start_date`` and
      ``finished_at <= end_date``. Those columns stay ``NULL`` until a worker scores
      the run, so a bound naturally excludes queued/in-progress runs.

    ``granularity`` controls how deep each run is serialized:
    ``platform_executions`` → ``scenario_results`` → ``metric_scores`` (see
    :func:`serialize_run`). Metric scores never carry their structured ``trace``;
    it is persisted for direct inspection but not surfaced here. Raises
    ``ValueError`` for an unknown granularity or an unparseable date. Results are
    ordered by ``BenchmarkRun.created_at``.
    """
    if granularity not in _GRANULARITIES:
        raise ValueError(
            f"Granularidad {granularity!r} inválida; use una de {_GRANULARITIES}."
        )
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    key: uuid.UUID | None = None
    if run_id is not None:
        try:
            key = uuid.UUID(run_id)
        except ValueError:
            return []

    async with session_scope(session) as db:
        runs = await run_repo.list_runs(
            db,
            run_key=key,
            platform=platform,
            start=start,
            end=end,
            with_metric_scores=granularity == GRANULARITY_METRIC,
        )
        return [serialize_run(run, granularity) for run in runs]


async def retrieve_platform_executions(
    *,
    platform: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Return the platform executions matching the given filters, as a flat list.

    One entry per :class:`PlatformExecution` rather than per run, so a run holding
    several platforms (files can carry a per-file platform override) yields several
    entries sharing a ``run_id``. Each entry is the platform's rollup — no nested
    scenario results; read those through :func:`retrieve_runs`.

    Both filters are optional and combined with AND:

    * ``platform`` — exact, case-sensitive match on ``PlatformExecution.platform``
      (e.g. ``"claude"``, ``"copilot"``, ``"gemini"``).
    * ``start_date`` / ``end_date`` — an ISO-8601 range (``YYYY-MM-DD`` or a full
      timestamp) over the **scoring window**, with the same semantics as
      :func:`retrieve_runs`: ``started_at >= start_date`` and
      ``finished_at <= end_date``. Those columns stay ``NULL`` until a worker scores
      the run, so a bound naturally excludes queued/in-progress executions.

    Raises ``ValueError`` for an unparseable date. Results are ordered by the run's
    creation date, then by platform; no match yields ``[]``.
    """
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    async with session_scope(session) as db:
        executions = await platform_execution_repo.list_platform_executions(
            db,
            platform=platform,
            start=start,
            end=end,
        )
        return [serialize_platform_execution(execution) for execution in executions]


def _parse_date(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 ``value`` (date or timestamp), or ``None`` when unset."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} {value!r} inválido; use un formato ISO-8601 (YYYY-MM-DD)."
        ) from exc
