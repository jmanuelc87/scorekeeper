"""Tests for ``core.services.ingestion``: the turn projection and persisting a run."""

from __future__ import annotations

import uuid
from io import BytesIO
from typing import ClassVar

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scorekeeper.core.metrics.base import (
    Metric,
    MetricResult,
    MetricTrace,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.core.metrics.catalog.hallucination import split_context_docs
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import JudgeVerdict
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.services import ingestion
from scorekeeper.core.services.ingestion import UploadedFile, ingest_evaluation, project_turns
from scorekeeper.core.services.runs import get_run_summary
from scorekeeper.core.services.scoring import run_evaluation
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    RetrievedContextDocument,
    ScenarioResult,
    SourceFile,
    Turn,
)


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


async def test_project_turns_pairs_user_and_model() -> None:
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


async def test_project_turns_missing_side_becomes_empty_string() -> None:
    turns = project_turns([{"turn": 1, "role": "user", "content": "solo pregunta"}])

    assert turns == [
        {
            "turn_number": 1,
            "prompt": "solo pregunta",
            "response": "",
            "retrieved_context_source": None,
            "expected_output": None,
        }
    ]


async def test_project_turns_carries_context_source_and_expected() -> None:
    messages = [
        {
            "turn": 1,
            "role": "user",
            "content": "pregunta",
            "retrieved_context_source": "fuente (https://h/a.pdf#page=1)",
            "expected_output": "respuesta ideal",
        },
        {"turn": 1, "role": "model", "content": "respuesta"},
    ]

    turns = project_turns(messages)

    assert turns[0]["retrieved_context_source"] == "fuente (https://h/a.pdf#page=1)"
    assert turns[0]["expected_output"] == "respuesta ideal"


async def test_ingest_stores_raw_context_source_without_extracting(session: AsyncSession) -> None:
    cell = "manual (https://ejemplo.com/manual.pdf#page=3)"
    content = _xlsx_bytes(
        ["turn", "role", "content", "retrieved_context"],
        [
            [1, "user", "pregunta", cell],
            [1, "model", "respuesta", ""],
        ],
    )
    files = [
        UploadedFile(
            filename="esc.xlsx", content=content, scenario_id="esc", use_case="default"
        )
    ]

    await ingest_evaluation("claude", files, session=session)

    # Ingest stores the raw cell and does NOT extract documents — that is the retrieval
    # stage's job (run later by the worker).
    docs = (await session.execute(select(RetrievedContextDocument))).scalars().all()
    assert docs == []
    turn = (
        await session.execute(
            select(Turn).options(selectinload(Turn.retrieved_documents))
        )
    ).scalars().one()
    assert turn.retrieved_context_source == cell
    assert turn.retrieved_documents == []


async def test_project_turns_joins_multiple_same_role_messages() -> None:
    messages = [
        {"turn": 1, "role": "user", "content": "línea 1"},
        {"turn": 1, "role": "user", "content": "línea 2"},
        {"turn": 1, "role": "model", "content": "ok"},
    ]

    turns = project_turns(messages)

    assert turns[0]["prompt"] == "línea 1\nlínea 2"


async def test_run_evaluation_single_platform(session: AsyncSession, registry) -> None:
    files = [
        UploadedFile(
            filename="esc1.xlsx",
            content=_conversation_bytes(),
            scenario_id="esc1",
            use_case="default",
        )
    ]

    summary = await run_evaluation(
        "claude", files, session=session, judge=RecordingJudge(0.8)
    )

    # Summary shape: a single platform in the platforms list.
    assert summary["status"] == "completado"
    assert len(summary["platforms"]) == 1
    assert summary["platforms"][0]["platform"] == "claude"
    assert summary["platforms"][0]["average_score"] == pytest.approx(0.8)

    # Persisted hierarchy: one PlatformExecution, one ScenarioResult per file,
    # two turns each, one MetricScore per turn.
    execs = (await session.execute(select(PlatformExecution))).scalars().all()
    assert {e.platform for e in execs} == {"claude"}
    scenarios = (await session.execute(select(ScenarioResult))).scalars().all()
    assert len(scenarios) == 1
    assert all(s.status == "completado" for s in scenarios)
    turn_count = (await session.execute(select(func.count()).select_from(Turn))).scalar_one()
    assert turn_count == 2
    score_count = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert score_count == 2  # 2 turns × 1 metric

    # Provenance recorded.
    sources = (await session.execute(select(SourceFile))).scalars().all()
    assert [s.filename for s in sources] == ["esc1.xlsx"]
    turn = (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().first()
    assert turn.prompt == "hola" and turn.response == "qué tal"


async def test_run_evaluation_multiple_files(session: AsyncSession, registry) -> None:
    files = [
        UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default"),
        UploadedFile("esc2.xlsx", _conversation_bytes(), "esc2", "default"),
    ]

    await run_evaluation("claude", files, session=session, judge=RecordingJudge())

    scenarios = (await session.execute(select(ScenarioResult))).scalars().all()
    assert len(scenarios) == 2  # 1 platform × 2 files
    assert {s.scenario_id for s in scenarios} == {"esc1", "esc2"}


async def test_ingest_resolves_the_platform_per_file(session: AsyncSession, registry) -> None:
    # esc1 has no override (falls back to the run-level "claude"); esc2 and esc3
    # override to "gemini". Each file gets its own execution carrying that platform.
    files = [
        UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default"),
        UploadedFile("esc2.xlsx", _conversation_bytes(), "esc2", "default", platform="gemini"),
        UploadedFile("esc3.xlsx", _conversation_bytes(), "esc3", "default", platform="gemini"),
    ]

    run_id = await ingest_evaluation("claude", files, session=session)

    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    by_scenario = {
        s.scenario_id: s.platform_executions[0].platform for s in run.scenario_results
    }
    assert by_scenario == {"esc1": "claude", "esc2": "gemini", "esc3": "gemini"}


async def test_ingest_falls_back_to_the_run_platform(session: AsyncSession, registry) -> None:
    files = [
        UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default"),
        UploadedFile("esc2.xlsx", _conversation_bytes(), "esc2", "default"),
    ]

    run_id = await ingest_evaluation("claude", files, session=session)

    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    # No per-file platform → every scenario carries the run-level one.
    assert len(run.scenario_results) == 2
    assert {
        pe.platform for s in run.scenario_results for pe in s.platform_executions
    } == {"claude"}


async def test_run_evaluation_rejects_empty_inputs(session: AsyncSession, registry) -> None:
    file = UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")
    with pytest.raises(ValueError):
        await run_evaluation("", [file], session=session, judge=RecordingJudge())
    with pytest.raises(ValueError):
        await run_evaluation("claude", [], session=session, judge=RecordingJudge())


async def test_run_evaluation_malformed_sheet_raises(session: AsyncSession, registry) -> None:
    bad = UploadedFile(
        "esc1.xlsx", _xlsx_bytes(["foo", "bar"], [["a", "b"]]), "esc1", "default"
    )
    with pytest.raises(ValueError):
        await run_evaluation("claude", [bad], session=session, judge=RecordingJudge())


async def test_ingest_sets_ingested_status(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]

    run_id = await ingest_evaluation("claude", files, session=session)

    # The tree exists, is ingested but not started, and is not yet scored.
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == "ingerido"
    turns = (await session.execute(select(Turn))).scalars().all()
    assert len(turns) == 2
    assert all(t.turn_score is None for t in turns)
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 0


async def test_ingest_progress_zero(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)

    summary = await get_run_summary(run_id, session=session)
    assert summary["progress"] == {"done": 0, "total": 2, "ratio": 0.0}


async def test_progress_midway(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)

    # Simulate a worker part-way through: running, one of two turns scored.
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    run.status = "en_proceso"
    turns = (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().all()
    turns[0].turn_score = 0.5
    await session.commit()

    summary = await get_run_summary(run_id, session=session)
    assert summary["progress"] == {"done": 1, "total": 2, "ratio": 0.5}


async def test_captured_messages_reach_turns_without_a_spreadsheet(session: AsyncSession) -> None:
    """An UploadedFile carrying messages skips the .xlsx parser end to end."""
    run_id = await ingest_evaluation(
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

    scenario = (
        await session.scalars(
            select(ScenarioResult).options(
                selectinload(ScenarioResult.platform_executions).selectinload(
                    PlatformExecution.turns
                )
            )
        )
    ).one()
    assert scenario.scenario_id == "esc-captura"
    platform_exec = scenario.platform_executions[0]
    assert platform_exec.source_ref == "https://claude.ai/chat/abc"
    # Turns were derived from role alternation: two user+model pairs.
    turns = sorted(platform_exec.turns, key=lambda t: t.turn_number)
    assert [(t.turn_number, t.prompt, t.response) for t in turns] == [
        (1, "Hola", "¿Qué tal?"),
        (2, "Adiós", "Hasta luego"),
    ]
    assert platform_exec.raw_conversation["messages"][0]["turn"] == 1
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert str(run.status) == "ingerido"


async def test_captured_model_lands_on_the_execution(session: AsyncSession) -> None:
    """The capturing client's model is stored per conversation; without one it is NULL."""
    await ingest_evaluation(
        "claude",
        [
            UploadedFile(
                filename="https://claude.ai/chat/abc",
                content=b"{}",
                scenario_id="esc-con-modelo",
                messages=[{"role": "user", "content": "Hola"}],
                model_name="Claude Opus 4.5",
            ),
            UploadedFile(
                filename="https://claude.ai/chat/def",
                content=b"{}",
                scenario_id="esc-sin-modelo",
                messages=[{"role": "user", "content": "Hola"}],
            ),
        ],
        session=session,
    )

    executions = (
        await session.scalars(
            select(PlatformExecution)
            .join(PlatformExecution.scenario_result)
            .order_by(ScenarioResult.scenario_id)
        )
    ).all()
    assert [(pe.scenario_result.scenario_id, pe.model_name) for pe in executions] == [
        ("esc-con-modelo", "Claude Opus 4.5"),
        ("esc-sin-modelo", None),
    ]


async def test_captured_citations_reach_turn_context(session: AsyncSession) -> None:
    """Sources scraped off a chat UI land on the turn as ``retrieved_context_source``.

    Mirrors what the extension's Copilot adapter produces: a turn whose answer
    arrives in two model messages (an interstitial, then the real answer) with the
    citations attached to the second one.
    """
    context = (
        "eleconomista.com.mx\nhttps://www.eleconomista.com.mx/tags/grupo-mexico-861"
        "\n\nforbes.com\nhttps://www.forbes.com/lists/global2000/"
    )

    await ingest_evaluation(
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

    turn = (await session.scalars(select(PlatformExecution))).one().turns[0]
    # Both model messages joined into one response, and the context survived even
    # though it hung off the second of them.
    assert turn.response == "Conectar para continuar\nTres noticias"
    assert turn.retrieved_context_source == context
    # Two blank-line-separated blocks, so the metrics see two retrieved documents.
    assert len(split_context_docs(turn.retrieved_context_source)) == 2


async def test_ingest_rejects_an_unknown_use_case(session: AsyncSession, registry) -> None:
    with pytest.raises(ingestion.UnknownUseCaseError, match="Caso\\(s\\) de uso desconocido"):
        await ingest_evaluation(
            "claude",
            [
                UploadedFile(
                    filename="esc1.xlsx",
                    content=_conversation_bytes(),
                    scenario_id="esc1",
                    use_case="inexistente",
                )
            ],
            session=session,
        )

    # Rolled back: a rejected use case persists nothing, not even the SourceFile.
    runs = (await session.execute(select(func.count()).select_from(BenchmarkRun))).scalar_one()
    assert runs == 0


async def test_ingest_links_the_scenario_to_its_use_case_row(
    session: AsyncSession, registry
) -> None:
    await ingest_evaluation(
        "claude",
        [
            UploadedFile(
                filename="esc1.xlsx",
                content=_conversation_bytes(),
                scenario_id="esc1",
                use_case="default",
            )
        ],
        session=session,
    )

    scenario = (
        await session.scalars(
            select(ScenarioResult).options(selectinload(ScenarioResult.use_case))
        )
    ).one()
    # The FK resolves to the named row; the name is no longer stored on the scenario.
    assert scenario.use_case.name == "default"
