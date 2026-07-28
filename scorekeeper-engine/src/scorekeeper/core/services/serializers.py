"""Read models — the JSON-serializable projections of the run tree.

These live beside the services rather than in the API layer because the Celery
worker uses them too: ``score_run`` returns :func:`summarize_run`, and the
worker has no API layer to borrow from. They emit plain dicts, which several
endpoints return verbatim — do not turn them into Pydantic models without
checking the wire format first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from scorekeeper.core.metrics.base import is_not_applicable
from scorekeeper.core.runner import (
    STATUS_COMPLETADO,
    STATUS_FALLIDO,
    STATUS_PARCIAL,
)
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    ScenarioResult,
    Turn,
    TurnTokenUsage,
)

# Serialization depth: how deep :func:`serialize_run` walks the run tree.
GRANULARITY_PLATFORM = "platform_executions"  # stop at the platform execution
GRANULARITY_SCENARIO = "scenario_results"  # descend into scenario results
GRANULARITY_METRIC = "metric_scores"  # descend through turns to metric scores
_GRANULARITIES = (GRANULARITY_PLATFORM, GRANULARITY_SCENARIO, GRANULARITY_METRIC)

_TERMINAL_STATUSES = {STATUS_COMPLETADO, STATUS_PARCIAL, STATUS_FALLIDO}


def _run_progress(run: BenchmarkRun) -> dict[str, Any]:
    """Turn-level progress of a run: how many turns have been scored so far.

    A turn counts as done once ``run_turn`` sets its ``turn_score``. While the run is
    still queued/running the ratio climbs live; once it reaches a terminal status it
    is pinned to 1.0 — a turn whose every metric failed keeps ``turn_score = None``
    (skip-metric-continue), so a finished run must not read < 100% forever.
    """
    turns = [
        turn
        for platform_exec in run.platform_executions
        for scenario in platform_exec.scenario_results
        for turn in scenario.turns
    ]
    total = len(turns)
    if run.status in _TERMINAL_STATUSES:
        done = total
    else:
        done = sum(turn.turn_score is not None for turn in turns)
    return {"done": done, "total": total, "ratio": round(done / total, 4) if total else 0.0}


def summarize_run(run: BenchmarkRun) -> dict[str, Any]:
    """Project the scored run into the JSON-serializable API response shape.

    A run may hold one ``PlatformExecution`` per distinct platform (files can carry
    a per-file platform override), so ``platforms`` is a list.
    """
    return {
        "run_id": str(run.id),
        "status": run.status,
        "progress": _run_progress(run),
        "platforms": [
            _platform_summary(platform_exec)
            for platform_exec in run.platform_executions
        ],
    }


def _platform_summary(platform_exec: PlatformExecution) -> dict[str, Any]:
    """One platform execution's rollup: average, scenario count, status breakdown."""
    breakdown: dict[str, int] = {}
    for scenario in platform_exec.scenario_results:
        breakdown[scenario.status] = breakdown.get(scenario.status, 0) + 1
    return {
        "platform": platform_exec.platform,
        "average_score": platform_exec.average_score,
        "scenarios": len(platform_exec.scenario_results),
        "status_breakdown": breakdown,
    }


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 string for a timestamp column, or ``None`` when unset."""
    return value.isoformat() if value is not None else None


def serialize_run(run: BenchmarkRun, granularity: str) -> dict[str, Any]:
    """Project a run into a JSON-serializable dict, deepened to ``granularity``.

    ``platform_executions`` emits run + per-platform rollups; ``scenario_results``
    adds each scenario; ``metric_scores`` adds each turn and its metric scores. All
    rollup values are already-persisted columns — nothing is recomputed here.
    """
    return {
        "run_id": str(run.id),
        "status": run.status,
        "created_at": _iso(run.created_at),
        "progress": _run_progress(run),
        "platforms": [
            _serialize_platform(platform_exec, granularity)
            for platform_exec in run.platform_executions
        ],
    }


def _serialize_platform(
    platform_exec: PlatformExecution, granularity: str
) -> dict[str, Any]:
    breakdown: dict[str, int] = {}
    for scenario in platform_exec.scenario_results:
        breakdown[scenario.status] = breakdown.get(scenario.status, 0) + 1
    entry: dict[str, Any] = {
        "platform": platform_exec.platform,
        "average_score": platform_exec.average_score,
        "started_at": _iso(platform_exec.started_at),
        "finished_at": _iso(platform_exec.finished_at),
        "scenarios": len(platform_exec.scenario_results),
        "status_breakdown": breakdown,
    }
    if granularity in (GRANULARITY_SCENARIO, GRANULARITY_METRIC):
        entry["scenario_results"] = [
            _serialize_scenario(scenario, granularity)
            for scenario in platform_exec.scenario_results
        ]
    return entry


def _serialize_scenario(
    scenario: ScenarioResult, granularity: str
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        # The row's UUID — the handle GET /scenarios/{id}/turns takes (distinct from the
        # human-readable, non-unique ``scenario_id`` label below).
        "id": str(scenario.id),
        "scenario_id": scenario.scenario_id,
        "use_case": scenario.use_case.name,
        # The model that answered, when the capturing client reported one; null for
        # every .xlsx import.
        "model_name": scenario.model_name,
        "status": scenario.status,
        "average_score": scenario.average_score,
    }
    if granularity == GRANULARITY_METRIC:
        entry["turns"] = [_serialize_turn(turn) for turn in scenario.turns]
    return entry


def _serialize_metric_score(score: MetricScore) -> dict[str, Any]:
    """One metric's score on a turn, without its structured trace.

    The stored not-applicable sentinel is a negative number — an internal encoding
    for "this metric had nothing to measure here". It surfaces as ``null`` so no
    client mistakes it for a score.
    """
    return {
        "metric_name": score.metric_name,
        "score": None if is_not_applicable(score.score) else score.score,
        "judge_model": score.judge_model,
        "rubric_version": score.rubric_version,
    }


def serialize_metric_trace(
    score: MetricScore, include_provenance: bool
) -> dict[str, Any]:
    """One metric's trace for the per-turn traces endpoint (steps, optional provenance)."""
    entry: dict[str, Any] = {"metric_name": score.metric_name}
    if include_provenance:
        entry["judge_model"] = score.judge_model
        entry["rubric_version"] = score.rubric_version
    entry["trace"] = {"steps": score.trace.steps} if score.trace is not None else None
    return entry


def serialize_turn_token_usage(turn: Turn) -> dict[str, Any]:
    """A turn's raw token usage; zeros when the turn has no usage row yet.

    ``total_tokens`` is derived (``input + output``), matching ``TurnTokenUsage`` which
    never stores the total.
    """
    usage = turn.token_usage or TurnTokenUsage(input_tokens=0, output_tokens=0)
    return {
        "turn_id": str(turn.id),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }


def serialize_scenario_turn(turn: Turn) -> dict[str, Any]:
    """Full turn view for the per-scenario turns endpoint: content + scores.

    Unlike :func:`_serialize_turn` (the ``/runs`` metric-granularity projection, which
    carries only ids/scores), this surfaces the conversation content — ``prompt``,
    ``response``, ``expected_output`` and the raw ``retrieved_context_source`` — so a
    caller can read the scenario's turns without re-uploading the source. The
    structured metric ``trace`` is still not surfaced here (read it via
    ``/turns/{turn_id}/traces``).
    """
    return {
        "turn_id": str(turn.id),
        "turn_number": turn.turn_number,
        "prompt": turn.prompt,
        "response": turn.response,
        "expected_output": turn.expected_output,
        "retrieved_context_source": turn.retrieved_context_source,
        "turn_score": turn.turn_score,
        "metric_scores": [_serialize_metric_score(score) for score in turn.metric_scores],
    }


def _serialize_turn(turn: Turn) -> dict[str, Any]:
    # The structured ``trace`` is intentionally not surfaced here: it is persisted
    # on the ``metric_traces`` table for direct inspection, but the read surface
    # (HTTP ``/runs``) does not expose it.
    return {
        "turn_id": str(turn.id),
        "turn_number": turn.turn_number,
        "turn_score": turn.turn_score,
        "metric_scores": [_serialize_metric_score(score) for score in turn.metric_scores],
    }
