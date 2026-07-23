"""Tests for the evaluation glue (``scorekeeper.evaluation``) and its endpoint.

Covers three layers: the pure ``project_turns`` projection, the ``run_evaluation``
orchestration end-to-end on in-memory SQLite with a stub judge and an isolated
metric registry, and the multipart ``POST /evaluations`` endpoint (with
``run_evaluation`` stubbed) for request parsing and error mapping.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from io import BytesIO
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scorekeeper import evaluation, tasks
from scorekeeper.api import app
from scorekeeper.database import (
    Base,
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    ScenarioResult,
    SourceFile,
    Turn,
)
from scorekeeper.database import MetricTrace as MetricTraceRow
from scorekeeper.evaluation import (
    UploadedFile,
    get_run_summary,
    ingest_evaluation,
    project_turns,
    retrieve_runs,
    retrieve_turn_traces,
    run_evaluation,
    score_run,
)
from scorekeeper.metrics.base import (
    Metric,
    MetricResult,
    MetricTrace,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.metrics.catalog.hallucination import split_context_docs
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry
from scorekeeper.metrics.scale import Unit


# --- Fakes --------------------------------------------------------------------


class RecordingJudge:
    """A Judge stub returning a fixed verdict (mirrors tests/test_runner.py)."""

    def __init__(self, score_value: float = 0.8, model: str = "judge-test") -> None:
        self.score_value = score_value
        self.model = model

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


# --- Fixtures & helpers -------------------------------------------------------


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _no_turn_delay(monkeypatch) -> None:
    """Skip the real between-turns pacing sleep so tests stay fast and deterministic."""
    monkeypatch.setattr("scorekeeper.runner.time.sleep", lambda *_: None)


@pytest.fixture
def registry():
    """Register a single fake metric (no scenarios → the 'default' set)."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
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


# --- project_turns ------------------------------------------------------------


def test_project_turns_pairs_user_and_model() -> None:
    messages = [
        {"turn": 1, "role": "user", "content": "hola"},
        {"turn": 1, "role": "model", "content": "qué tal"},
        {"turn": 2, "role": "user", "content": "adiós"},
        {"turn": 2, "role": "model", "content": "hasta luego"},
    ]

    turns = project_turns(messages)

    assert [t["turn_number"] for t in turns] == [1, 2]
    assert turns[0]["prompt"] == "hola"
    assert turns[0]["response"] == "qué tal"
    assert turns[1]["prompt"] == "adiós"
    assert turns[1]["response"] == "hasta luego"


def test_project_turns_missing_side_becomes_empty_string() -> None:
    turns = project_turns([{"turn": 1, "role": "user", "content": "solo pregunta"}])

    assert turns == [
        {
            "turn_number": 1,
            "prompt": "solo pregunta",
            "response": "",
            "retrieved_context": None,
            "expected_output": None,
        }
    ]


def test_project_turns_carries_context_and_expected() -> None:
    messages = [
        {
            "turn": 1,
            "role": "user",
            "content": "pregunta",
            "retrieved_context": "doc recuperado",
            "expected_output": "respuesta ideal",
        },
        {"turn": 1, "role": "model", "content": "respuesta"},
    ]

    turns = project_turns(messages)

    assert turns[0]["retrieved_context"] == "doc recuperado"
    assert turns[0]["expected_output"] == "respuesta ideal"


def test_project_turns_joins_multiple_same_role_messages() -> None:
    messages = [
        {"turn": 1, "role": "user", "content": "línea 1"},
        {"turn": 1, "role": "user", "content": "línea 2"},
        {"turn": 1, "role": "model", "content": "ok"},
    ]

    turns = project_turns(messages)

    assert turns[0]["prompt"] == "línea 1\nlínea 2"


# --- run_evaluation -----------------------------------------------------------


def test_run_evaluation_single_platform(session: Session, registry) -> None:
    files = [
        UploadedFile(
            filename="esc1.xlsx",
            content=_conversation_bytes(),
            scenario_id="esc1",
            use_case="default",
        )
    ]

    summary = run_evaluation(
        "claude", files, session=session, judge=RecordingJudge(0.8)
    )

    # Summary shape: a single platform in the platforms list.
    assert summary["status"] == "completado"
    assert len(summary["platforms"]) == 1
    assert summary["platforms"][0]["platform"] == "claude"
    assert summary["platforms"][0]["average_score"] == pytest.approx(0.8)

    # Persisted hierarchy: one PlatformExecution, one ScenarioResult per file,
    # two turns each, one MetricScore per turn.
    execs = session.execute(select(PlatformExecution)).scalars().all()
    assert {e.platform for e in execs} == {"claude"}
    scenarios = session.execute(select(ScenarioResult)).scalars().all()
    assert len(scenarios) == 1
    assert all(s.status == "completado" for s in scenarios)
    turn_count = session.execute(select(func.count()).select_from(Turn)).scalar_one()
    assert turn_count == 2
    score_count = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()
    assert score_count == 2  # 2 turns × 1 metric

    # Provenance recorded.
    sources = session.execute(select(SourceFile)).scalars().all()
    assert [s.filename for s in sources] == ["esc1.xlsx"]
    turn = session.execute(select(Turn).order_by(Turn.turn_number)).scalars().first()
    assert turn.prompt == "hola" and turn.response == "qué tal"


def test_run_evaluation_multiple_files(session: Session, registry) -> None:
    files = [
        UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default"),
        UploadedFile("esc2.xlsx", _conversation_bytes(), "esc2", "default"),
    ]

    run_evaluation("claude", files, session=session, judge=RecordingJudge())

    scenarios = session.execute(select(ScenarioResult)).scalars().all()
    assert len(scenarios) == 2  # 1 platform × 2 files
    assert {s.scenario_id for s in scenarios} == {"esc1", "esc2"}


def test_ingest_groups_files_by_per_file_platform(session: Session, registry) -> None:
    # esc1 has no override (falls back to the run-level "claude"); esc2 overrides to
    # "gemini"; esc3 also overrides to "gemini" and must share esc2's execution.
    files = [
        UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default"),
        UploadedFile("esc2.xlsx", _conversation_bytes(), "esc2", "default", platform="gemini"),
        UploadedFile("esc3.xlsx", _conversation_bytes(), "esc3", "default", platform="gemini"),
    ]

    run_id = ingest_evaluation("claude", files, session=session)

    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    by_platform = {pe.platform: pe for pe in run.platform_executions}
    # One execution per distinct resolved platform.
    assert set(by_platform) == {"claude", "gemini"}
    assert {s.scenario_id for s in by_platform["claude"].scenario_results} == {"esc1"}
    assert {s.scenario_id for s in by_platform["gemini"].scenario_results} == {"esc2", "esc3"}


def test_ingest_single_platform_when_no_overrides(session: Session, registry) -> None:
    files = [
        UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default"),
        UploadedFile("esc2.xlsx", _conversation_bytes(), "esc2", "default"),
    ]

    run_id = ingest_evaluation("claude", files, session=session)

    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    # No per-file platform → a single execution holding both scenarios.
    assert len(run.platform_executions) == 1
    assert run.platform_executions[0].platform == "claude"
    assert len(run.platform_executions[0].scenario_results) == 2


def test_run_evaluation_rejects_empty_inputs(session: Session, registry) -> None:
    file = UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")
    with pytest.raises(ValueError):
        run_evaluation("", [file], session=session, judge=RecordingJudge())
    with pytest.raises(ValueError):
        run_evaluation("claude", [], session=session, judge=RecordingJudge())


def test_run_evaluation_malformed_sheet_raises(session: Session, registry) -> None:
    bad = UploadedFile(
        "esc1.xlsx", _xlsx_bytes(["foo", "bar"], [["a", "b"]]), "esc1", "default"
    )
    with pytest.raises(ValueError):
        run_evaluation("claude", [bad], session=session, judge=RecordingJudge())


# --- ingest_evaluation / score_run (the async split) --------------------------


def test_ingest_sets_queued_status(session: Session, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]

    run_id = ingest_evaluation("claude", files, session=session)

    # The tree exists, is queued, and is not yet scored.
    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == "en_cola"
    turns = session.execute(select(Turn)).scalars().all()
    assert len(turns) == 2
    assert all(t.turn_score is None for t in turns)
    scores = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()
    assert scores == 0


def test_ingest_progress_zero(session: Session, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = ingest_evaluation("claude", files, session=session)

    summary = get_run_summary(run_id, session=session)
    assert summary["progress"] == {"done": 0, "total": 2, "ratio": 0.0}


def test_progress_midway(session: Session, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = ingest_evaluation("claude", files, session=session)

    # Simulate a worker part-way through: running, one of two turns scored.
    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    run.status = "en_proceso"
    turns = session.execute(select(Turn).order_by(Turn.turn_number)).scalars().all()
    turns[0].turn_score = 0.5
    session.commit()

    summary = get_run_summary(run_id, session=session)
    assert summary["progress"] == {"done": 1, "total": 2, "ratio": 0.5}


def test_score_run_end_to_end(session: Session, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = ingest_evaluation("claude", files, session=session)

    summary = score_run(run_id, session=session, judge=RecordingJudge(0.8))

    assert summary["run_id"] == run_id
    assert summary["status"] == "completado"
    assert summary["platforms"][0]["average_score"] == pytest.approx(0.8)
    # Fully scored -> progress pinned to 1.0.
    assert summary["progress"] == {"done": 2, "total": 2, "ratio": 1.0}
    scores = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()
    assert scores == 2  # 2 turns × 1 metric


def test_score_run_unknown_id_raises(session: Session) -> None:
    with pytest.raises(ValueError):
        score_run("00000000-0000-0000-0000-000000000000", session=session)


def test_get_run_summary_unknown_returns_none(session: Session) -> None:
    assert get_run_summary("not-a-uuid", session=session) is None
    assert get_run_summary("00000000-0000-0000-0000-000000000000", session=session) is None


# --- POST /evaluations endpoint -----------------------------------------------


def _payload(**over) -> str:
    body = {"platform": "claude", "use_case": "default"}
    body.update(over)
    return json.dumps(body)


def test_endpoint_ingests_enqueues_and_returns_run_id(monkeypatch) -> None:
    captured: dict = {}

    def fake_ingest(platform, files, *, session=None):
        captured["platform"] = platform
        captured["files"] = files
        return "run-123"

    monkeypatch.setattr(evaluation, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_score_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": _payload(files={"esc1.xlsx": {"scenario_id": "custom", "use_case": "web_search"}})},
        )

    # 202 Accepted with the run id; scoring was enqueued, not run inline.
    assert response.status_code == 202
    body = response.json()
    assert body == {"run_id": "run-123", "status": "en_cola"}
    assert captured["enqueued"] == "run-123"
    # Per-file overrides applied; defaults elsewhere.
    assert captured["platform"] == "claude"
    upload = captured["files"][0]
    assert upload.scenario_id == "custom"
    assert upload.use_case == "web_search"
    assert upload.platform is None  # no per-file platform → falls back to payload


def test_endpoint_per_file_platform_override(monkeypatch) -> None:
    captured: dict = {}

    def fake_ingest(platform, files, *, session=None):
        captured["platform"] = platform
        captured["files"] = {u.filename: u for u in files}
        return "run-9"

    monkeypatch.setattr(evaluation, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_score_run", lambda run_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[
                ("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream")),
                ("files", ("esc2.xlsx", _conversation_bytes(), "application/octet-stream")),
            ],
            data={"payload": _payload(files={"esc2.xlsx": {"platform": "gemini"}})},
        )

    assert response.status_code == 202
    # Payload platform is the fallback; only esc2 overrides it.
    assert captured["platform"] == "claude"
    assert captured["files"]["esc1.xlsx"].platform is None
    assert captured["files"]["esc2.xlsx"].platform == "gemini"


def test_endpoint_defaults_scenario_id_to_stem(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        evaluation,
        "ingest_evaluation",
        lambda platform, files, **kw: captured.update(files=files) or "run-x",
    )
    monkeypatch.setattr(tasks, "enqueue_score_run", lambda run_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": _payload()},
        )

    assert response.status_code == 202
    upload = captured["files"][0]
    assert upload.scenario_id == "esc1"  # stem of esc1.xlsx
    assert upload.use_case == "default"


def test_endpoint_get_returns_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        evaluation,
        "get_run_summary",
        lambda run_id, **kw: {
            "run_id": run_id,
            "status": "en_proceso",
            "progress": {"done": 1, "total": 2, "ratio": 0.5},
            "platforms": [
                {
                    "platform": "claude",
                    "average_score": None,
                    "scenarios": 1,
                    "status_breakdown": {},
                }
            ],
        },
    )

    with TestClient(app) as client:
        response = client.get("/evaluations/run-123")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-123"
    assert body["status"] == "en_proceso"
    assert body["progress"] == {"done": 1, "total": 2, "ratio": 0.5}
    assert body["platforms"][0]["average_score"] is None


def test_endpoint_get_unknown_run_404(monkeypatch) -> None:
    monkeypatch.setattr(evaluation, "get_run_summary", lambda run_id, **kw: None)

    with TestClient(app) as client:
        response = client.get("/evaluations/does-not-exist")

    assert response.status_code == 404


def test_runs_endpoint_forwards_filters_and_returns_list(monkeypatch) -> None:
    captured: dict = {}
    runs = [{"run_id": "run-1", "status": "completado", "platforms": []}]

    def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return runs

    monkeypatch.setattr(evaluation, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get(
            "/runs",
            params={
                "run_id": "run-1",
                "platform": "claude",
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
                "granularity": "metric_scores",
            },
        )

    assert response.status_code == 200
    assert response.json() == runs
    # Every query param is forwarded to retrieve_runs.
    assert captured == {
        "run_id": "run-1",
        "platform": "claude",
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "granularity": "metric_scores",
    }


def test_runs_endpoint_defaults_granularity_and_empty_result(monkeypatch) -> None:
    captured: dict = {}

    def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(evaluation, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get("/runs")

    assert response.status_code == 200
    assert response.json() == []
    # No filters given; granularity defaults to scenario_results.
    assert captured == {
        "run_id": None,
        "platform": None,
        "start_date": None,
        "end_date": None,
        "granularity": "scenario_results",
    }


def test_runs_endpoint_invalid_input_400(monkeypatch) -> None:
    def fake_retrieve(**kwargs):
        raise ValueError("Granularidad 'nope' inválida")

    monkeypatch.setattr(evaluation, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get("/runs", params={"granularity": "nope"})

    assert response.status_code == 400


def test_endpoint_rejects_invalid_payload() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": "no-es-json"},
        )
    assert response.status_code == 422


def test_endpoint_rejects_non_xlsx() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[("files", ("esc1.csv", b"data", "text/csv"))],
            data={"payload": _payload()},
        )
    assert response.status_code == 400


def test_endpoint_rejects_empty_file() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[("files", ("esc1.xlsx", b"", "application/octet-stream"))],
            data={"payload": _payload()},
        )
    assert response.status_code == 400


def test_endpoint_maps_value_error_to_400(monkeypatch) -> None:
    def fake_ingest(*args, **kwargs):
        raise ValueError("hoja inválida")

    monkeypatch.setattr(evaluation, "ingest_evaluation", fake_ingest)

    with TestClient(app) as client:
        response = client.post(
            "/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": _payload()},
        )
    assert response.status_code == 400


# --- retrieve_runs ------------------------------------------------------------


def _score_one(session: Session, *, platform: str = "claude", scenario_id: str = "esc1") -> str:
    """Ingest + score a single-file run, returning its run_id."""
    files = [UploadedFile(f"{scenario_id}.xlsx", _conversation_bytes(), scenario_id, "default")]
    summary = run_evaluation(platform, files, session=session, judge=RecordingJudge(0.8))
    return summary["run_id"]


def test_retrieve_platform_granularity_stops_at_platform(session: Session, registry) -> None:
    run_id = _score_one(session)

    runs = retrieve_runs(granularity="platform_executions", session=session)

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
    assert "scenario_results" not in platform


def test_retrieve_scenario_granularity_adds_scenarios(session: Session, registry) -> None:
    _score_one(session)

    runs = retrieve_runs(granularity="scenario_results", session=session)

    scenarios = runs[0]["platforms"][0]["scenario_results"]
    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert scenario["scenario_id"] == "esc1"
    assert scenario["use_case"] == "default"
    assert scenario["status"] == "completado"
    # Scenario granularity does not descend into turns.
    assert "turns" not in scenario


def test_retrieve_metric_granularity_adds_turns_and_scores(session: Session, registry) -> None:
    _score_one(session)

    runs = retrieve_runs(granularity="metric_scores", session=session)

    scenario = runs[0]["platforms"][0]["scenario_results"][0]
    turns = scenario["turns"]
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
    trace_rows = session.execute(select(func.count()).select_from(MetricTraceRow)).scalar_one()
    metric_rows = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()
    assert trace_rows == metric_rows


# --- retrieve_turn_traces + /turns/{turn_id}/traces ---------------------------


def test_retrieve_turn_traces_with_provenance(session: Session, registry) -> None:
    _score_one(session)
    turn = session.execute(select(Turn).order_by(Turn.turn_number)).scalars().first()

    traces = retrieve_turn_traces(str(turn.id), session=session)

    assert len(traces) == 1
    entry = traces[0]
    assert entry["metric_name"] == "utilidad"
    assert entry["judge_model"] == "judge-test"
    assert entry["rubric_version"] == "v1"
    # The full structured trace is surfaced here (unlike the general read paths).
    assert entry["trace"]["steps"][0]["entries"][0]["value"] == pytest.approx(0.8)


def test_retrieve_turn_traces_minimal_omits_provenance(session: Session, registry) -> None:
    _score_one(session)
    turn = session.execute(select(Turn).order_by(Turn.turn_number)).scalars().first()

    traces = retrieve_turn_traces(str(turn.id), include_provenance=False, session=session)

    entry = traces[0]
    assert set(entry) == {"metric_name", "trace"}
    assert entry["metric_name"] == "utilidad"
    assert entry["trace"]["steps"][0]["entries"][0]["value"] == pytest.approx(0.8)


def test_retrieve_turn_traces_unknown_or_malformed_is_none(session: Session, registry) -> None:
    _score_one(session)

    assert retrieve_turn_traces("not-a-uuid", session=session) is None
    assert (
        retrieve_turn_traces(
            "00000000-0000-0000-0000-000000000000", session=session
        )
        is None
    )


def test_turn_traces_endpoint_forwards_and_returns(monkeypatch) -> None:
    captured: dict = {}
    payload = [{"metric_name": "utilidad", "trace": {"steps": []}}]

    def fake(turn_id, **kwargs):
        captured["turn_id"] = turn_id
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(evaluation, "retrieve_turn_traces", fake)

    with TestClient(app) as client:
        response = client.get("/turns/abc/traces", params={"provenance": "false"})

    assert response.status_code == 200
    assert response.json() == payload
    assert captured == {"turn_id": "abc", "include_provenance": False}


def test_turn_traces_endpoint_unknown_turn_404(monkeypatch) -> None:
    monkeypatch.setattr(evaluation, "retrieve_turn_traces", lambda *a, **k: None)

    with TestClient(app) as client:
        response = client.get("/turns/nope/traces")

    assert response.status_code == 404


def test_mcp_retrieve_turn_traces_coalesces_none(monkeypatch) -> None:
    from scorekeeper import server

    monkeypatch.setattr(evaluation, "retrieve_turn_traces", lambda *a, **k: None)
    # The MCP tool never 404s: an unknown turn becomes an empty list.
    assert server.retrieve_turn_traces("nope") == []


def test_retrieve_default_granularity_is_scenario(session: Session, registry) -> None:
    _score_one(session)

    runs = retrieve_runs(session=session)

    platform = runs[0]["platforms"][0]
    assert "scenario_results" in platform
    assert "turns" not in platform["scenario_results"][0]


def test_retrieve_platform_filter(session: Session, registry) -> None:
    _score_one(session, platform="claude")

    assert len(retrieve_runs(platform="claude", session=session)) == 1
    # Exact match: a different platform yields nothing.
    assert retrieve_runs(platform="gemini", session=session) == []


def test_retrieve_run_id_filter(session: Session, registry) -> None:
    run_id = _score_one(session, scenario_id="esc1")
    _score_one(session, scenario_id="esc2")

    runs = retrieve_runs(run_id=run_id, session=session)
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id
    # Unknown / malformed ids resolve to an empty list, never an error.
    assert retrieve_runs(run_id="not-a-uuid", session=session) == []
    assert retrieve_runs(run_id="00000000-0000-0000-0000-000000000000", session=session) == []


def test_retrieve_date_range_filters_scoring_window(session: Session, registry) -> None:
    _score_one(session)
    platform_exec = session.execute(select(PlatformExecution)).scalars().one()
    platform_exec.started_at = datetime(2026, 7, 10, 12, 0, 0)
    platform_exec.finished_at = datetime(2026, 7, 10, 12, 5, 0)
    session.commit()

    # The scoring window falls inside July.
    assert len(retrieve_runs(start_date="2026-07-01", end_date="2026-07-31", session=session)) == 1
    # started_at (07-10) precedes an August lower bound -> excluded.
    assert retrieve_runs(start_date="2026-08-01", session=session) == []
    # finished_at (07-10) exceeds an early-July upper bound -> excluded.
    assert retrieve_runs(end_date="2026-07-05", session=session) == []


def test_retrieve_date_bound_excludes_unscored(session: Session, registry) -> None:
    ingest_evaluation(
        "claude",
        [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")],
        session=session,
    )

    # No date filter: the queued run is returned.
    assert len(retrieve_runs(session=session)) == 1
    # A date bound excludes it — started_at/finished_at stay NULL until scored.
    assert retrieve_runs(start_date="2020-01-01", session=session) == []


def test_retrieve_invalid_granularity_raises(session: Session) -> None:
    with pytest.raises(ValueError):
        retrieve_runs(granularity="turnos", session=session)


def test_retrieve_invalid_date_raises(session: Session) -> None:
    with pytest.raises(ValueError):
        retrieve_runs(start_date="ayer", session=session)


# --- POST /captures -----------------------------------------------------------


def test_captures_endpoint_ingests_and_enqueues(monkeypatch) -> None:
    captured: dict = {}

    def fake_ingest(platform, files, *, session=None):
        captured["platform"] = platform
        captured["files"] = files
        return "run-cap"

    monkeypatch.setattr(evaluation, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_score_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post(
            "/captures",
            json={
                "platform": "gemini",
                "use_case": "web_search",
                "conversations": [
                    {
                        "scenario_id": "esc-navegador",
                        "source_ref": "https://gemini.google.com/app/abc",
                        "messages": [
                            {"role": "user", "content": "¿Cuál es la capital?"},
                            {"role": "model", "content": "Madrid."},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-cap", "status": "en_cola"}
    assert captured["enqueued"] == "run-cap"
    assert captured["platform"] == "gemini"
    upload = captured["files"][0]
    assert upload.scenario_id == "esc-navegador"
    assert upload.use_case == "web_search"  # payload default, no per-conversation one
    assert upload.filename == "https://gemini.google.com/app/abc"
    assert upload.messages == [
        {"role": "user", "content": "¿Cuál es la capital?"},
        {"role": "model", "content": "Madrid."},
    ]
    # No file bytes exist, so the provenance hash covers the capture itself.
    assert json.loads(upload.content.decode()) == upload.messages


def test_captures_endpoint_per_conversation_overrides(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        evaluation,
        "ingest_evaluation",
        lambda platform, files, **kw: captured.update(files=files) or "run-cap",
    )
    monkeypatch.setattr(tasks, "enqueue_score_run", lambda run_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/captures",
            json={
                "platform": "claude",
                "conversations": [
                    {
                        "scenario_id": "esc1",
                        "platform": "copilot",
                        "use_case": "document_retrieval",
                        "messages": [{"role": "user", "content": "Hola"}],
                    }
                ],
            },
        )

    assert response.status_code == 202
    upload = captured["files"][0]
    assert upload.platform == "copilot"
    assert upload.use_case == "document_retrieval"
    # Without a source_ref the scenario id names the "file".
    assert upload.filename == "esc1"


def test_captures_endpoint_rejects_contentless_conversation() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/captures",
            json={
                "platform": "claude",
                "conversations": [
                    {"scenario_id": "vacio", "messages": [{"role": "user", "content": "  "}]}
                ],
            },
        )

    assert response.status_code == 400
    assert "vacio" in response.json()["detail"]


def test_captures_endpoint_requires_conversations() -> None:
    with TestClient(app) as client:
        response = client.post("/captures", json={"platform": "claude", "conversations": []})

    assert response.status_code == 422


def test_captured_messages_reach_turns_without_a_spreadsheet(session: Session) -> None:
    """An UploadedFile carrying messages skips the .xlsx parser end to end."""
    run_id = ingest_evaluation(
        "claude",
        [
            UploadedFile(
                filename="https://claude.ai/chat/abc",
                content=b"{}",
                scenario_id="esc-captura",
                messages=[
                    {"role": "user", "content": "Hola"},
                    {"role": "model", "content": "¿Qué tal?"},
                    {"role": "user", "content": "Adiós"},
                    {"role": "model", "content": "Hasta luego"},
                ],
            )
        ],
        session=session,
    )

    scenario = session.scalars(select(ScenarioResult)).one()
    assert scenario.scenario_id == "esc-captura"
    assert scenario.source_ref == "https://claude.ai/chat/abc"
    # Turns were derived from role alternation: two user+model pairs.
    turns = sorted(scenario.turns, key=lambda t: t.turn_number)
    assert [(t.turn_number, t.prompt, t.response) for t in turns] == [
        (1, "Hola", "¿Qué tal?"),
        (2, "Adiós", "Hasta luego"),
    ]
    assert scenario.raw_conversation["messages"][0]["turn"] == 1
    assert str(session.get(BenchmarkRun, uuid.UUID(run_id)).status) == "en_cola"


def test_captured_citations_reach_turn_context(session: Session) -> None:
    """Sources scraped off a chat UI land on the turn as ``retrieved_context``.

    Mirrors what the extension's Copilot adapter produces: a turn whose answer
    arrives in two model messages (an interstitial, then the real answer) with the
    citations attached to the second one.
    """
    context = (
        "eleconomista.com.mx\nhttps://www.eleconomista.com.mx/tags/grupo-mexico-861"
        "\n\nforbes.com\nhttps://www.forbes.com/lists/global2000/"
    )

    ingest_evaluation(
        "copilot",
        [
            UploadedFile(
                filename="https://m365.cloud.microsoft/chat/",
                content=b"{}",
                scenario_id="esc-citas",
                messages=[
                    {"role": "user", "content": "Noticias sobre Grupo México"},
                    {"role": "model", "content": "Conectar para continuar"},
                    {"role": "model", "content": "Tres noticias", "retrieved_context": context},
                ],
            )
        ],
        session=session,
    )

    turn = session.scalars(select(ScenarioResult)).one().turns[0]
    # Both model messages joined into one response, and the context survived even
    # though it hung off the second of them.
    assert turn.response == "Conectar para continuar\nTres noticias"
    assert turn.retrieved_context == context
    # Two blank-line-separated blocks, so the metrics see two retrieved documents.
    assert len(split_context_docs(turn.retrieved_context)) == 2
