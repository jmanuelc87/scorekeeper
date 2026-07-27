"""Tests for ``core.services.scoring`` and the run lifecycle in ``core.services.runs``."""

from __future__ import annotations

import uuid
from io import BytesIO
from typing import ClassVar

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.base import (
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
from scorekeeper.core.services.runs import get_run_summary, set_turn_selection, start_run
from scorekeeper.core.services.scoring import score_run
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    Turn,
)


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


async def _all_turn_ids(session: AsyncSession, run_id: str) -> list[str]:
    turns = (await session.execute(select(Turn))).scalars().all()
    return [str(t.id) for t in turns]


async def test_score_run_end_to_end(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    # Scoring is opt-in per turn — select both turns before scoring.
    await set_turn_selection(
        run_id, await _all_turn_ids(session, run_id), True, session=session
    )

    summary = await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    assert summary["run_id"] == run_id
    assert summary["status"] == "completado"
    assert summary["platforms"][0]["average_score"] == pytest.approx(0.8)
    # Fully scored -> progress pinned to 1.0.
    assert summary["progress"] == {"done": 2, "total": 2, "ratio": 1.0}
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 2  # 2 turns × 1 metric


async def test_score_run_only_scores_selected_turns(session: AsyncSession, registry) -> None:
    # Select only the first of the two ingested turns; scoring must skip the other.
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    first_turn = (
        (await session.execute(select(Turn).order_by(Turn.turn_number))).scalars().first()
    )
    await set_turn_selection(run_id, [str(first_turn.id)], True, session=session)

    await score_run(run_id, session=session, judge=RecordingJudge(0.8))

    # Only the selected turn produced a score row; the other stays unscored.
    scores = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert scores == 1
    by_number = {
        t.turn_number: t.turn_score
        for t in (await session.execute(select(Turn))).scalars().all()
    }
    assert by_number[1] == pytest.approx(0.8)
    assert by_number[2] is None


async def test_set_turn_selection_updates_matching_turns(
    session: AsyncSession, registry
) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    turn_ids = await _all_turn_ids(session, run_id)

    updated = await set_turn_selection(run_id, turn_ids, True, session=session)

    assert updated == 2
    assert all(t.is_selected for t in (await session.execute(select(Turn))).scalars().all())

    # Deselect one turn; the count reflects only the turns actually touched.
    updated = await set_turn_selection(run_id, [turn_ids[0]], False, session=session)
    assert updated == 1
    by_id = {
        str(t.id): t.is_selected
        for t in (await session.execute(select(Turn))).scalars().all()
    }
    assert by_id[turn_ids[0]] is False
    assert by_id[turn_ids[1]] is True


async def test_set_turn_selection_ignores_foreign_and_bad_ids(
    session: AsyncSession, registry
) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)

    updated = await set_turn_selection(
        run_id,
        ["not-a-uuid", "00000000-0000-0000-0000-000000000000"],
        True,
        session=session,
    )

    assert updated == 0
    assert not any(
        t.is_selected for t in (await session.execute(select(Turn))).scalars().all()
    )


async def test_set_turn_selection_unknown_run_returns_none(session: AsyncSession) -> None:
    assert await set_turn_selection("not-a-uuid", [], True, session=session) is None
    assert (
        await set_turn_selection(
            "00000000-0000-0000-0000-000000000000", [], True, session=session
        )
        is None
    )


async def test_set_turn_selection_after_start_raises(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    await start_run(run_id, session=session)  # ingerido -> en_cola

    # Selection is only allowed before a run leaves the 'ingerido' state.
    with pytest.raises(ValueError):
        await set_turn_selection(
            run_id, await _all_turn_ids(session, run_id), True, session=session
        )


async def test_score_run_unknown_id_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await score_run("00000000-0000-0000-0000-000000000000", session=session)


async def test_get_run_summary_unknown_returns_none(session: AsyncSession) -> None:
    assert await get_run_summary("not-a-uuid", session=session) is None
    assert await get_run_summary("00000000-0000-0000-0000-000000000000", session=session) is None


async def test_start_run_moves_ingested_to_queued(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)

    status = await start_run(run_id, session=session)

    assert status == "en_cola"
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == "en_cola"


async def test_start_run_unknown_returns_none(session: AsyncSession) -> None:
    assert await start_run("not-a-uuid", session=session) is None
    assert await start_run("00000000-0000-0000-0000-000000000000", session=session) is None


async def test_start_run_already_started_raises(session: AsyncSession, registry) -> None:
    files = [UploadedFile("esc1.xlsx", _conversation_bytes(), "esc1", "default")]
    run_id = await ingest_evaluation("claude", files, session=session)
    await start_run(run_id, session=session)  # ingerido -> en_cola

    # A second start is rejected so a run is never enqueued twice.
    with pytest.raises(ValueError):
        await start_run(run_id, session=session)
