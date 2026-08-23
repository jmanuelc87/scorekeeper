"""Tests for ``core.services.read_models``: the reads behind the results endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from io import BytesIO
from typing import ClassVar

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.base import (
    NOT_APPLICABLE,
    Metric,
    MetricResult,
    MetricTrace,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import JudgeVerdict
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.services.ingestion import UploadedFile, ingest_evaluation
from scorekeeper.core.services.read_models import (
    retrieve_platform_executions,
    retrieve_run_scenarios,
    retrieve_runs,
    retrieve_scenario_turns,
    retrieve_turn_token_usage,
    retrieve_turn_traces,
)
from scorekeeper.core.services.scoring import run_evaluation
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    ScenarioResult,
    Turn,
)
from scorekeeper.db.models import MetricTrace as MetricTraceRow


class RecordingJudge:
    """A Judge stub returning a fixed verdict (mirrors tests/test_runner.py)."""

    def __init__(self, score_value: float = 0.8, model: str = "judge-test") -> None:
        self.score_value = score_value
        self.model = model

    def model_for(self, step=None) -> str:
        return self.model

    def score(self, *, rubric, turn, scale, rubric_version=None, step=None) -> JudgeVerdict:
        return JudgeVerdict(score=self.score_value, justification="razón", model=self.model)

    def structured(self, *, instruction, turn, schema, step=None):  # pragma: no cover - unused
        raise NotImplementedError

    def embed(self, *, texts):  # pragma: no cover - unused
        raise NotImplementedError


class _FakeMetric(Metric):
    category = MetricCategory.RAG
    scale = Unit()
    rubric: ClassVar[str] = "¿Es buena la respuesta?"

    def evaluate(self, turn: TurnView, judge) -> MetricResult:
        verdict = judge.score(rubric=self.rubric, turn=turn, scale=self.scale)
        return MetricResult(
            metric_name=self.name,
            raw_score=verdict.score,
            normalized_score=self.normalize(verdict.score),
            trace=MetricTrace(
                steps=[
                    TraceStep(
                        label="Puntuación",
                        entries=[
                            TraceEntry(
                                label=self.name,
                                value=verdict.score,
                                justification=verdict.justification,
                            )
                        ],
                    )
                ]
            ),
            judge_model=verdict.model,
            rubric_version=self.rubric_version,
        )


class Utilidad(_FakeMetric):
    name = "utilidad"


@pytest.fixture
async def registry(session: AsyncSession, compose_use_case):
    """Register a single fake metric and link it to the ``default`` use case.

    Metric selection is user data now: no metric declares a use case, so an upload
    ingested under ``"default"`` scores nothing until a set links the two.
    """
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    await compose_use_case([Utilidad.name])
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


def _xlsx_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _conversation_bytes() -> bytes:
    return _xlsx_bytes(
        ["turn", "role", "content"],
        [
            [1, "user", "hola"],
            [1, "model", "qué tal"],
            [2, "user", "adiós"],
            [2, "model", "hasta luego"],
        ],
    )


async def _score_one(
    session: AsyncSession, *, platform: str = "claude", scenario_id: str = "esc1"
) -> str:
    """Ingest + score a single-file run, returning its run_id."""
    files = [UploadedFile(f"{scenario_id}.xlsx", _conversation_bytes(), scenario_id, "default")]
    summary = await run_evaluation(platform, files, session=session, judge=RecordingJudge(0.8))
    return summary["run_id"]


async def test_retrieve_platform_granularity_stops_at_platform(session: AsyncSession, registry) -> None:
    run_id = await _score_one(session)

    runs = await retrieve_runs(granularity="platform_executions", session=session)

    assert len(runs) == 1
    run = runs[0]
    assert run["run_id"] == run_id
    assert run["status"] == "completado"
    assert run["created_at"] is not None
    platform = run["platforms"][0]
    assert platform["platform"] == "claude"
    assert platform["average_score"] == pytest.approx(0.8)
    assert platform["status_breakdown"] == {"completado": 1}
    # Platform granularity does not descend into scenarios.
    assert "scenario_results" not in run


async def test_retrieve_scenario_granularity_adds_scenarios(session: AsyncSession, registry) -> None:
    await _score_one(session)

    runs = await retrieve_runs(granularity="scenario_results", session=session)

    scenarios = runs[0]["scenario_results"]
    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert scenario["scenario_id"] == "esc1"
    assert scenario["use_case"] == "default"
    assert scenario["status"] == "completado"
    # The conversation itself hangs off the scenario, one per platform.
    execution = scenario["platform_executions"][0]
    assert execution["platform"] == "claude"
    # A spreadsheet never names the model that answered.
    assert execution["model_name"] is None
    # Scenario granularity does not descend into turns.
    assert "turns" not in execution


async def test_scenario_serialization_exposes_the_captured_model(
    session: AsyncSession, registry
) -> None:
    """A capture that reported a model surfaces it in the scenario projection."""
    await ingest_evaluation(
        "claude",
        [
            UploadedFile(
                filename="https://claude.ai/chat/abc",
                content=b"{}",
                scenario_id="esc-modelo",
                messages=[{"role": "user", "content": "hola"}],
                model_name="Claude Opus 4.5",
            )
        ],
        session=session,
    )

    runs = await retrieve_runs(granularity="scenario_results", session=session)

    scenario = runs[0]["scenario_results"][0]
    assert scenario["platform_executions"][0]["model_name"] == "Claude Opus 4.5"


async def test_retrieve_metric_granularity_adds_turns_and_scores(session: AsyncSession, registry) -> None:
    await _score_one(session)

    runs = await retrieve_runs(granularity="metric_scores", session=session)

    scenario = runs[0]["scenario_results"][0]
    turns = scenario["platform_executions"][0]["turns"]
    assert [t["turn_number"] for t in turns] == [1, 2]
    # Each serialized turn carries its id so clients can reach /turns/{id}/traces.
    assert all(uuid.UUID(t["turn_id"]) for t in turns)
    scores = turns[0]["metric_scores"]
    assert len(scores) == 1
    assert scores[0]["metric_name"] == "utilidad"
    assert scores[0]["score"] == pytest.approx(0.8)
    assert scores[0]["judge_model"] == "judge-test"
    # The structured trace is never surfaced (nor the old flattened justification),
    # even though a trace row is persisted per metric score for direct inspection.
    assert "trace" not in scores[0]
    assert "justification" not in scores[0]
    trace_rows = (await session.execute(select(func.count()).select_from(MetricTraceRow))).scalar_one()
    metric_rows = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert trace_rows == metric_rows


async def test_retrieve_turn_traces_with_provenance(session: AsyncSession, registry) -> None:
    await _score_one(session)
    turn = (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().first()

    traces = await retrieve_turn_traces(str(turn.id), session=session)

    assert len(traces) == 1
    entry = traces[0]
    assert entry["metric_name"] == "utilidad"
    assert entry["judge_model"] == "judge-test"
    assert entry["rubric_version"] == "v1"
    # The full structured trace is surfaced here (unlike the general read paths).
    assert entry["trace"]["steps"][0]["entries"][0]["value"] == pytest.approx(0.8)


async def test_retrieve_turn_traces_minimal_omits_provenance(session: AsyncSession, registry) -> None:
    await _score_one(session)
    turn = (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().first()

    traces = await retrieve_turn_traces(str(turn.id), include_provenance=False, session=session)

    entry = traces[0]
    assert set(entry) == {"metric_name", "trace"}
    assert entry["metric_name"] == "utilidad"
    assert entry["trace"]["steps"][0]["entries"][0]["value"] == pytest.approx(0.8)


async def test_retrieve_turn_traces_unknown_or_malformed_is_none(session: AsyncSession, registry) -> None:
    await _score_one(session)

    assert await retrieve_turn_traces("not-a-uuid", session=session) is None
    assert (
        await retrieve_turn_traces(
            "00000000-0000-0000-0000-000000000000", session=session
        )
        is None
    )


async def test_retrieve_turn_token_usage_returns_row(session: AsyncSession, registry) -> None:
    await _score_one(session)
    turn = (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().first()
    turn.token_usage.input_tokens = 120
    turn.token_usage.output_tokens = 45
    await session.flush()

    usage = await retrieve_turn_token_usage(str(turn.id), session=session)

    assert usage == {
        "turn_id": str(turn.id),
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,  # derived input + output, never stored.
    }


async def test_retrieve_turn_token_usage_without_row_reports_zeros(
    session: AsyncSession, registry
) -> None:
    await _score_one(session)
    turn = (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().first()
    # A turn with no usage row (e.g. never scored) reports zeros rather than 404ing.
    turn.token_usage = None
    await session.flush()

    usage = await retrieve_turn_token_usage(str(turn.id), session=session)

    assert usage == {
        "turn_id": str(turn.id),
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


async def test_retrieve_turn_token_usage_unknown_or_malformed_is_none(
    session: AsyncSession, registry
) -> None:
    await _score_one(session)

    assert await retrieve_turn_token_usage("not-a-uuid", session=session) is None
    assert (
        await retrieve_turn_token_usage(
            "00000000-0000-0000-0000-000000000000", session=session
        )
        is None
    )


async def test_retrieve_scenario_turns_returns_content_and_scores(
    session: AsyncSession, registry
) -> None:
    await _score_one(session)
    scenario = (await session.execute(select(ScenarioResult))).scalars().one()

    turns = await retrieve_scenario_turns(str(scenario.id), session=session)

    assert [t["turn_number"] for t in turns] == [1, 2]
    first = turns[0]
    # Conversation content is surfaced (unlike the /runs metric projection).
    assert first["prompt"] == "hola"
    assert first["response"] == "qué tal"
    assert uuid.UUID(first["turn_id"])
    assert first["turn_score"] == pytest.approx(0.8)
    scores = first["metric_scores"]
    assert scores[0]["metric_name"] == "utilidad"
    assert scores[0]["score"] == pytest.approx(0.8)
    assert scores[0]["judge_model"] == "judge-test"
    # The structured trace is not surfaced here (read it via /turns/{id}/traces).
    assert "trace" not in scores[0]


async def test_not_applicable_score_serializes_as_null(
    session: AsyncSession, registry
) -> None:
    await _score_one(session)
    scenario = (await session.execute(select(ScenarioResult))).scalars().one()
    # A metric that had nothing to measure stores the negative sentinel.
    first_turn = (
        (await session.execute(select(Turn).where(Turn.turn_number == 1)))
        .scalars()
        .one()
    )
    score_row = (
        (
            await session.execute(
                select(MetricScore).where(MetricScore.turn_id == first_turn.id)
            )
        )
        .scalars()
        .one()
    )
    score_row.score = NOT_APPLICABLE
    await session.commit()

    turns = await retrieve_scenario_turns(str(scenario.id), session=session)
    runs = await retrieve_runs(granularity="metric_scores", session=session)

    # Both projections hide the internal encoding behind a null.
    assert turns[0]["metric_scores"][0]["score"] is None
    run_turns = runs[0]["scenario_results"][0]["platform_executions"][0]["turns"]
    assert run_turns[0]["metric_scores"][0]["score"] is None


async def test_scenario_serialization_exposes_id(session: AsyncSession, registry) -> None:
    await _score_one(session)
    scenario = (await session.execute(select(ScenarioResult))).scalars().one()

    runs = await retrieve_runs(granularity="scenario_results", session=session)

    serialized = runs[0]["scenario_results"][0]
    # The UUID handle GET /scenarios/{id}/turns takes, alongside the readable label.
    assert serialized["id"] == str(scenario.id)
    assert serialized["scenario_id"] == "esc1"


async def test_retrieve_scenario_turns_empty_scenario_returns_list(
    session: AsyncSession, compose_use_case
) -> None:
    scenario = ScenarioResult(
        scenario_id="vacio", use_case=await compose_use_case([]), run=BenchmarkRun()
    )
    PlatformExecution(platform="claude", scenario_result=scenario)
    session.add(scenario)
    await session.commit()

    # A scenario with no turns yields [] (not None — it exists).
    assert await retrieve_scenario_turns(str(scenario.id), session=session) == []


async def test_retrieve_scenario_turns_unknown_or_malformed_is_none(
    session: AsyncSession, registry
) -> None:
    await _score_one(session)

    assert await retrieve_scenario_turns("not-a-uuid", session=session) is None
    assert (
        await retrieve_scenario_turns(
            "00000000-0000-0000-0000-000000000000", session=session
        )
        is None
    )


async def test_retrieve_default_granularity_is_scenario(session: AsyncSession, registry) -> None:
    await _score_one(session)

    runs = await retrieve_runs(session=session)

    assert "scenario_results" in runs[0]
    assert "turns" not in runs[0]["scenario_results"][0]["platform_executions"][0]


async def test_retrieve_platform_filter(session: AsyncSession, registry) -> None:
    await _score_one(session, platform="claude")

    assert len(await retrieve_runs(platform="claude", session=session)) == 1
    # Exact match: a different platform yields nothing.
    assert await retrieve_runs(platform="gemini", session=session) == []


async def test_retrieve_run_id_filter(session: AsyncSession, registry) -> None:
    run_id = await _score_one(session, scenario_id="esc1")
    await _score_one(session, scenario_id="esc2")

    runs = await retrieve_runs(run_id=run_id, session=session)
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id
    # Unknown / malformed ids resolve to an empty list, never an error.
    assert await retrieve_runs(run_id="not-a-uuid", session=session) == []
    assert await retrieve_runs(run_id="00000000-0000-0000-0000-000000000000", session=session) == []


async def test_retrieve_date_range_filters_scoring_window(session: AsyncSession, registry) -> None:
    await _score_one(session)
    platform_exec = (await session.execute(select(PlatformExecution))).scalars().one()
    platform_exec.started_at = datetime(2026, 7, 10, 12, 0, 0)
    platform_exec.finished_at = datetime(2026, 7, 10, 12, 5, 0)
    await session.commit()

    # The scoring window falls inside July.
    assert len(await retrieve_runs(start_date="2026-07-01", end_date="2026-07-31", session=session)) == 1
    # started_at (07-10) precedes an August lower bound -> excluded.
    assert await retrieve_runs(start_date="2026-08-01", session=session) == []
    # finished_at (07-10) exceeds an early-July upper bound -> excluded.
    assert await retrieve_runs(end_date="2026-07-05", session=session) == []


async def test_retrieve_date_bound_excludes_unscored(session: AsyncSession, registry) -> None:
    await ingest_evaluation(
        "claude",
        [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")],
        session=session,
    )

    # No date filter: the queued run is returned.
    assert len(await retrieve_runs(session=session)) == 1
    # A date bound excludes it — started_at/finished_at stay NULL until scored.
    assert await retrieve_runs(start_date="2020-01-01", session=session) == []


async def test_retrieve_invalid_granularity_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await retrieve_runs(granularity="turnos", session=session)


async def test_retrieve_invalid_date_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await retrieve_runs(start_date="ayer", session=session)


async def _score_two(session: AsyncSession, *, platform: str = "claude") -> str:
    """Ingest + score a two-file run (two scenarios), returning its run_id."""
    files = [
        UploadedFile(f"{scenario_id}.xlsx", _conversation_bytes(), scenario_id, "default")
        for scenario_id in ("esc1", "esc2")
    ]
    summary = await run_evaluation(platform, files, session=session, judge=RecordingJudge(0.8))
    return summary["run_id"]


async def test_retrieve_run_scenarios_returns_flat_rollups(
    session: AsyncSession, registry
) -> None:
    run_id = await _score_two(session)

    scenarios = await retrieve_run_scenarios(run_id, session=session)

    assert scenarios is not None
    # One entry per scenario, each nesting the conversations it was run as.
    assert [scenario["scenario_id"] for scenario in scenarios] == ["esc1", "esc2"]
    assert set(scenarios[0]) == {
        "id",
        "scenario_id",
        "use_case",
        "status",
        "platform_executions",
    }
    assert scenarios[0]["use_case"] == "default"
    # No scenario-level average: the score lives on each platform's execution.
    assert "average_score" not in scenarios[0]
    # The platform, model and per-conversation rollup live on the execution.
    execution = scenarios[0]["platform_executions"][0]
    assert set(execution) == {
        "id",
        "platform",
        "model_name",
        "status",
        "average_score",
        "started_at",
        "finished_at",
    }
    assert execution["platform"] == "claude"
    assert execution["average_score"] == pytest.approx(0.8)
    # The id is the ScenarioResult UUID the turns endpoint takes.
    assert await retrieve_scenario_turns(scenarios[0]["id"], session=session) is not None


async def test_retrieve_run_scenarios_scopes_to_its_own_run(
    session: AsyncSession, registry
) -> None:
    run_id = await _score_one(session, scenario_id="esc1")
    await _score_one(session, scenario_id="esc2")

    scenarios = await retrieve_run_scenarios(run_id, session=session)

    assert [scenario["scenario_id"] for scenario in scenarios] == ["esc1"]


async def test_retrieve_run_scenarios_platform_filter(
    session: AsyncSession, registry
) -> None:
    run_id = await _score_one(session, platform="claude")

    assert len(await retrieve_run_scenarios(run_id, platform="claude", session=session)) == 1
    # Exact match: a different platform yields nothing — but the run still exists.
    assert await retrieve_run_scenarios(run_id, platform="gemini", session=session) == []


async def test_retrieve_run_scenarios_status_filter(
    session: AsyncSession, registry
) -> None:
    run_id = await _score_one(session)
    scenarios = await retrieve_run_scenarios(run_id, session=session)
    status = scenarios[0]["status"]

    assert len(await retrieve_run_scenarios(run_id, status=status, session=session)) == 1
    assert await retrieve_run_scenarios(run_id, status="pendiente", session=session) == []


async def test_retrieve_run_scenarios_unknown_or_malformed_is_none(
    session: AsyncSession,
) -> None:
    # None (-> 404) is what distinguishes an unknown run from a run with no matches.
    assert await retrieve_run_scenarios("not-a-uuid", session=session) is None
    assert (
        await retrieve_run_scenarios(
            "00000000-0000-0000-0000-000000000000", session=session
        )
        is None
    )


async def test_retrieve_run_scenarios_unscored_run_returns_entries(
    session: AsyncSession, registry
) -> None:
    """An ingested-but-unscored run still lists its scenarios, with null rollups."""
    run_id = await ingest_evaluation(
        "claude",
        [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")],
        session=session,
    )

    scenarios = await retrieve_run_scenarios(run_id, session=session)

    assert len(scenarios) == 1
    assert scenarios[0]["status"] == "pending"
    assert scenarios[0]["platform_executions"][0]["average_score"] is None


async def test_retrieve_platform_executions_returns_flat_entries(
    session: AsyncSession, registry
) -> None:
    run_id = await _score_one(session)

    rows = await retrieve_platform_executions(session=session)

    assert len(rows) == 1
    row = rows[0]
    # Flat: the platform rollup is the whole entry, never a nested scenario tree.
    assert set(row) == {
        "run_id",
        "platform",
        "started_at",
        "finished_at",
        "average_score",
        "scenarios",
        "status_breakdown",
    }
    assert row["run_id"] == run_id
    assert row["platform"] == "claude"
    assert row["average_score"] == pytest.approx(0.8)
    assert row["scenarios"] == 1
    assert row["status_breakdown"] == {"completado": 1}
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


async def test_retrieve_platform_executions_platform_filter(
    session: AsyncSession, registry
) -> None:
    await _score_one(session, platform="claude")

    assert len(await retrieve_platform_executions(platform="claude", session=session)) == 1
    assert await retrieve_platform_executions(platform="gemini", session=session) == []
    # The match is case-sensitive.
    assert await retrieve_platform_executions(platform="Claude", session=session) == []


async def test_retrieve_platform_executions_date_range(
    session: AsyncSession, registry
) -> None:
    await _score_one(session)
    platform_exec = (await session.execute(select(PlatformExecution))).scalars().one()
    platform_exec.started_at = datetime(2026, 7, 10, 12, 0, 0)
    platform_exec.finished_at = datetime(2026, 7, 10, 12, 5, 0)
    await session.commit()

    # The scoring window falls inside July.
    assert (
        len(
            await retrieve_platform_executions(
                start_date="2026-07-01", end_date="2026-07-31", session=session
            )
        )
        == 1
    )
    assert await retrieve_platform_executions(start_date="2026-08-01", session=session) == []
    assert await retrieve_platform_executions(end_date="2026-07-05", session=session) == []
    # Both bounds are inclusive.
    assert (
        len(await retrieve_platform_executions(start_date="2026-07-10T12:00:00", session=session))
        == 1
    )
    assert (
        len(await retrieve_platform_executions(end_date="2026-07-10T12:05:00", session=session))
        == 1
    )


async def test_retrieve_platform_executions_unfinished_execution(
    session: AsyncSession, compose_use_case
) -> None:
    scenario = ScenarioResult(
        scenario_id="esc1", use_case=await compose_use_case([]), run=BenchmarkRun()
    )
    PlatformExecution(
        platform="claude",
        started_at=datetime(2026, 7, 10, 12, 0, 0),
        scenario_result=scenario,
    )
    session.add(scenario)
    await session.commit()

    rows = await retrieve_platform_executions(session=session)
    assert len(rows) == 1
    # Started but never finished: the rollup counts its conversation, end still open.
    assert rows[0]["scenarios"] == 1
    assert rows[0]["status_breakdown"] == {"pending": 1}
    assert rows[0]["finished_at"] is None
    # The bounds are asymmetric: a lower bound only tests started_at, so an
    # in-progress execution survives it...
    assert len(await retrieve_platform_executions(start_date="2026-07-01", session=session)) == 1
    # ...but an upper bound tests finished_at, and NULL <= end is never true.
    assert await retrieve_platform_executions(end_date="2026-07-31", session=session) == []


async def test_retrieve_platform_executions_flattens_multiple_platforms(
    session: AsyncSession, compose_use_case
) -> None:
    run = BenchmarkRun()
    # One scenario run on both platforms — the shape the rollup exists to summarize.
    scenario = ScenarioResult(
        scenario_id="esc1", use_case=await compose_use_case([]), run=run
    )
    for platform in ("gemini", "claude"):
        PlatformExecution(platform=platform, scenario_result=scenario)
    session.add(scenario)
    await session.commit()

    rows = await retrieve_platform_executions(session=session)

    # One entry per distinct platform in the run — both sharing its run_id.
    assert len(rows) == 2
    assert {row["run_id"] for row in rows} == {str(run.id)}
    # Within a run the rollups are ordered by platform (inserted gemini first).
    assert [row["platform"] for row in rows] == ["claude", "gemini"]


async def test_retrieve_platform_executions_ordering(session: AsyncSession, registry) -> None:
    first = await _score_one(session, scenario_id="esc1")
    second = await _score_one(session, scenario_id="esc2")

    rows = await retrieve_platform_executions(session=session)

    # Ordered by the run's creation date, matching GET /runs.
    assert [row["run_id"] for row in rows] == [first, second]


async def test_retrieve_platform_executions_empty(session: AsyncSession) -> None:
    assert await retrieve_platform_executions(session=session) == []


async def test_retrieve_platform_executions_invalid_date_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await retrieve_platform_executions(start_date="ayer", session=session)
    with pytest.raises(ValueError):
        await retrieve_platform_executions(end_date="nunca", session=session)
