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
from scorekeeper.core.metrics.rollup import platform_average
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

# Serialization depth: how deep :func:`serialize_run` walks the run tree. The names are
# unchanged from when platform executions were the run's children — they still describe
# the depth of the *response*, which is what clients select on.
GRANULARITY_PLATFORM = "platform_executions"  # per-platform rollups only
GRANULARITY_SCENARIO = "scenario_results"  # add the run's scenario results
GRANULARITY_METRIC = "metric_scores"  # descend through turns to metric scores
_GRANULARITIES = (GRANULARITY_PLATFORM, GRANULARITY_SCENARIO, GRANULARITY_METRIC)

_TERMINAL_STATUSES = {STATUS_COMPLETADO, STATUS_PARCIAL, STATUS_FALLIDO}


def _run_progress(run: BenchmarkRun) -> dict[str, Any]:
    """Turn-level progress of a run: how many turns have been scored so far.

    A turn counts as done once ``run_turn`` sets its ``turn_score``. While the run is
    still queued/running the ratio climbs live; once it reaches a terminal status it
    is pinned to 1.0 — a turn whose every metric failed keeps ``turn_score = None``
    (skip-metric-continue), so a finished run must not read < 100% forever.

    Only selected turns are counted; deselected turns do not contribute to progress.
    """
    turns = [
        turn
        for scenario in run.scenario_results
        for platform_exec in scenario.platform_executions
        for turn in platform_exec.turns
    ]
    selected_turns = [turn for turn in turns if turn.is_selected]
    total = len(selected_turns)
    if run.status in _TERMINAL_STATUSES:
        done = total
    else:
        done = sum(turn.turn_score is not None for turn in selected_turns)
    return {"done": done, "total": total, "ratio": round(done / total, 4) if total else 0.0}


def summarize_run(run: BenchmarkRun) -> dict[str, Any]:
    """Project the scored run into the JSON-serializable API response shape.

    A run's scenarios may name different platforms (files can carry a per-file platform
    override), so ``platforms`` is a list — one entry per distinct platform.
    """
    return {
        "run_id": str(run.id),
        "status": run.status,
        "progress": _run_progress(run),
        "platforms": platform_rollups(run),
    }


def platform_rollups(run: BenchmarkRun) -> list[dict[str, Any]]:
    """One rollup per distinct platform in ``run``, ordered by platform name.

    The rollup has no table of its own: a platform execution belongs to one scenario, so
    "how did this platform do across this run" is a group-by over the run's executions
    rather than a stored row. ``scenarios`` counts the executions on that platform —
    equivalently, the scenarios that were run on it, since a scenario contributes at most
    one conversation per platform in the usual case.
    """
    grouped: dict[str, list[PlatformExecution]] = {}
    for scenario in run.scenario_results:
        for platform_exec in scenario.platform_executions:
            grouped.setdefault(platform_exec.platform, []).append(platform_exec)

    rollups = []
    for platform, executions in sorted(grouped.items()):
        breakdown: dict[str, int] = {}
        for platform_exec in executions:
            breakdown[platform_exec.status] = breakdown.get(platform_exec.status, 0) + 1
        started = [pe.started_at for pe in executions if pe.started_at is not None]
        finished = [pe.finished_at for pe in executions if pe.finished_at is not None]
        rollups.append(
            {
                "platform": platform,
                "average_score": platform_average(
                    [pe.average_score for pe in executions]
                ),
                # Null until *every* member has finished: a half-scored platform has no
                # end yet, which is what keeps the date filters excluding it.
                "started_at": _iso(min(started)) if started else None,
                "finished_at": (
                    _iso(max(finished)) if len(finished) == len(executions) else None
                ),
                "scenarios": len(executions),
                "status_breakdown": breakdown,
            }
        )
    return rollups


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 string for a timestamp column, or ``None`` when unset."""
    return value.isoformat() if value is not None else None


def serialize_run(run: BenchmarkRun, granularity: str) -> dict[str, Any]:
    """Project a run into a JSON-serializable dict, deepened to ``granularity``.

    ``platform_executions`` emits run + per-platform rollups; ``scenario_results``
    adds the run's scenarios; ``metric_scores`` adds each turn and its metric scores.

    Scenarios sit at the top level rather than nested under a platform, mirroring the
    schema: they are the run's children, and each names its own platform. The
    ``platforms`` block stays as the per-platform rollup clients summarize a run with.
    """
    entry: dict[str, Any] = {
        "run_id": str(run.id),
        "status": run.status,
        # The batch a capturing client grouped this run under; null for every other path.
        "label": run.label,
        "created_at": _iso(run.created_at),
        "progress": _run_progress(run),
        "platforms": platform_rollups(run),
    }
    if granularity in (GRANULARITY_SCENARIO, GRANULARITY_METRIC):
        entry["scenario_results"] = [
            serialize_scenario(scenario, granularity) for scenario in run.scenario_results
        ]
    return entry


def serialize_scenario(
    scenario: ScenarioResult, granularity: str = GRANULARITY_SCENARIO
) -> dict[str, Any]:
    """One scenario and how each platform answered it.

    Shared by ``GET /runs`` and ``GET /runs/{run_id}/scenarios``. The scenario carries a
    ``status`` rolled up across its ``platform_executions`` but **no average**: the
    scores worth reading are the per-platform ones, side by side. Turns are nested under
    their execution only at ``metric_scores``; otherwise read them via
    ``/scenarios/{scenario_id}/turns``.
    """
    return {
        # The row's UUID — the handle GET /scenarios/{id}/turns takes (distinct from the
        # human-readable, non-unique ``scenario_id`` label below).
        "id": str(scenario.id),
        "scenario_id": scenario.scenario_id,
        "use_case": scenario.use_case.name,
        "status": scenario.status,
        "platform_executions": [
            _serialize_execution(platform_exec, granularity)
            for platform_exec in scenario.platform_executions
        ],
    }


def _serialize_execution(
    platform_exec: PlatformExecution, granularity: str
) -> dict[str, Any]:
    """One captured conversation: which platform answered, how it scored, its turns."""
    entry: dict[str, Any] = {
        "id": str(platform_exec.id),
        "platform": platform_exec.platform,
        # The model that answered, when the capturing client reported one; null for
        # every .xlsx import.
        "model_name": platform_exec.model_name,
        "status": platform_exec.status,
        "average_score": platform_exec.average_score,
        "started_at": _iso(platform_exec.started_at),
        "finished_at": _iso(platform_exec.finished_at),
    }
    if granularity == GRANULARITY_METRIC:
        entry["turns"] = [_serialize_turn(turn) for turn in platform_exec.turns]
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
    """One metric's trace for the per-turn traces endpoint.

    Steps, the judge calls behind them, and optionally the provenance.
    """
    entry: dict[str, Any] = {"metric_name": score.metric_name}
    if include_provenance:
        entry["judge_model"] = score.judge_model
        entry["rubric_version"] = score.rubric_version
    entry["trace"] = {"steps": score.trace.steps} if score.trace is not None else None
    # One entry per LLM call behind the score, in ``sequence`` order (the relationship
    # is ordered by it): the exact prompt sent to the judge, which the trace's steps
    # only summarize. Empty for scores written before the calls were recorded.
    entry["judge_calls"] = [
        {
            "sequence": call.sequence,
            "step": call.step,
            "model": call.model,
            "system_prompt": call.system_prompt,
            "prompt": call.prompt,
            "latency_ms": call.latency_ms,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
        }
        for call in score.judge_calls
    ]
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

    A scenario spans several conversations, so each turn names the ``platform`` whose
    conversation it belongs to — otherwise the flat list could not be told apart.
    """
    return {
        "turn_id": str(turn.id),
        "platform": turn.platform_execution.platform,
        "turn_number": turn.turn_number,
        "prompt": turn.prompt,
        "response": turn.response,
        "expected_output": turn.expected_output,
        "retrieved_context_source": turn.retrieved_context_source,
        "turn_score": turn.turn_score,
        "is_selected": turn.is_selected,
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
        "is_selected": turn.is_selected,
        "metric_scores": [_serialize_metric_score(score) for score in turn.metric_scores],
    }
