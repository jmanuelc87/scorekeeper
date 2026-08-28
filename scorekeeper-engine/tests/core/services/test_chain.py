"""Tests for the chained evaluation — one unit of work is one turn.

Everything here is driven directly through ``start_chain`` / ``advance_chain``: the
Celery seam that enqueues what they return is covered in ``tests/test_tasks.py``. The
fake judge/metrics mirror ``tests/core/test_runner.py`` so no LLM is involved.
"""

from __future__ import annotations

import threading
import uuid
from typing import ClassVar

import pytest
from sqlalchemy import select
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
from scorekeeper.core.retrieval.types import (
    DocType,
    DocumentLocator,
    ExtractedContent,
    RetrievalOutcome,
    RetrievalReport,
    Sentence,
    SourceFormat,
    SourceRef,
)
from scorekeeper.core.runner import (
    MAX_TURN_ATTEMPTS,
    STATUS_COMPLETADO,
    STATUS_FALLIDO,
)
from scorekeeper.core.services import chain
from scorekeeper.core.services.chain import advance_chain, start_chain
from scorekeeper.core.services.status import STATUS_EN_PROCESO
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricDefinition,
    PlatformExecution,
    ScenarioResult,
    Turn,
    UseCase,
    UseCaseMetric,
)

USE_CASE = "soporte"


# --- Fakes --------------------------------------------------------------------


class RecordingJudge:
    """A Judge stub returning a fixed verdict and recording the turns it saw."""

    def __init__(self, score_value: float = 0.8, fail_on_prompt: str | None = None) -> None:
        self.score_value = score_value
        self.fail_on_prompt = fail_on_prompt
        self.seen_turns: list[TurnView] = []
        self._lock = threading.Lock()

    def model_for(self, step=None) -> str:
        return "judge-test"

    def score(self, *, rubric, turn, scale, rubric_version=None, step=None) -> JudgeVerdict:
        with self._lock:
            self.seen_turns.append(turn)
        if self.fail_on_prompt is not None and turn.prompt == self.fail_on_prompt:
            raise RuntimeError("fallo del juez")
        return JudgeVerdict(score=self.score_value, justification="razón", model="judge-test")

    def structured(self, *, instruction, turn, schema, step=None):  # pragma: no cover
        raise NotImplementedError

    def embed(self, *, texts):  # pragma: no cover
        raise NotImplementedError


class Utilidad(Metric):
    name = "utilidad"
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
                        entries=[TraceEntry(label=self.name, value=verdict.score)],
                    )
                ]
            ),
            judge_model=verdict.model,
        )


class _FakePipeline:
    """A RetrievalPipeline stub: each cell yields one doc echoing the cell text."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.purges: list[int] = []

    async def purge_cache(self) -> int:
        self.purges.append(len(self.calls))
        return len(self.calls)

    async def run(self, cell: str) -> RetrievalReport:
        self.calls.append(cell)
        source = SourceRef(name="ref", url="https://h/x.html", rank=0)
        locator = DocumentLocator(
            document_url="https://h/x.html",
            filename="x.html",
            doc_type=DocType.HTML,
            host="h",
        )
        outcome = RetrievalOutcome.assembled(
            source,
            locator,
            ExtractedContent(
                text=f"md::{cell}",
                sentences=[Sentence(page=None, index=0, text=f"md::{cell}")],
            ),
        )
        return RetrievalReport(source_format=SourceFormat.PLAINTEXT, outcomes=[outcome])


# --- Fixtures & helpers -------------------------------------------------------


@pytest.fixture
def registry():
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


async def _use_case(session: AsyncSession) -> UseCase:
    row = (
        await session.execute(select(UseCase).where(UseCase.name == USE_CASE))
    ).scalars().one_or_none()
    if row is None:
        row = UseCase(name=USE_CASE)
        session.add(row)
        await session.flush()
        metric = MetricDefinition(name="utilidad")
        session.add(metric)
        await session.flush()
        session.add(UseCaseMetric(use_case_id=row.id, metric_id=metric.id))
        await session.flush()
    return row


async def _seed_run(
    session: AsyncSession,
    shape: dict[str, dict[str, list[tuple[str, str]]]],
    *,
    selected: set[str] | None = None,
    context: bool = False,
) -> BenchmarkRun:
    """Build a run tree from ``{platform: {scenario_id: [(prompt, response), ...]}}``.

    ``selected`` names the turns to flag for scoring as ``"platform/scenario/number"``;
    ``None`` selects every turn.
    """
    use_case = await _use_case(session)
    run = BenchmarkRun()
    # A scenario is shared across the platforms that ran it, so build it once and hang
    # one execution per platform off it.
    scenarios: dict[str, ScenarioResult] = {}
    for platform, shape_scenarios in shape.items():
        for scenario_id, exchanges in shape_scenarios.items():
            scenario = scenarios.get(scenario_id)
            if scenario is None:
                scenario = ScenarioResult(
                    scenario_id=scenario_id, use_case=use_case, run=run
                )
                scenarios[scenario_id] = scenario
            platform_exec = PlatformExecution(
                platform=platform, scenario_result=scenario
            )
            for i, (prompt, response) in enumerate(exchanges, start=1):
                key = f"{platform}/{scenario_id}/{i}"
                platform_exec.turns.append(
                    Turn(
                        turn_number=i,
                        prompt=prompt,
                        response=response,
                        is_selected=selected is None or key in selected,
                        retrieved_context_source=f"ref-{key}" if context else None,
                    )
                )
    session.add(run)
    await session.commit()
    return run


async def _drive(session: AsyncSession, run: BenchmarkRun, judge=None, pipeline=None):
    """Run the whole chain to completion, returning the turn ids in visit order."""
    visited: list[str] = []
    nxt = await start_chain(str(run.id), session=session)
    while nxt is not None:
        visited.append(nxt.turn_id)
        nxt = await advance_chain(
            nxt.turn_id, session=session, judge=judge, pipeline=pipeline
        )
    return visited


# --- start_chain --------------------------------------------------------------


async def test_start_chain_marks_en_proceso_and_returns_the_first_turn(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})

    nxt = await start_chain(str(run.id), session=session)

    assert run.status == STATUS_EN_PROCESO
    assert nxt is not None
    assert nxt.turn_id == str(run.scenario_results[0].platform_executions[0].turns[0].id)
    assert nxt.countdown == 0.0  # nothing to pace away from yet


async def test_start_chain_without_selected_turns_finalizes_the_run(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b")]}}, selected=set())

    nxt = await start_chain(str(run.id), session=session)

    assert nxt is None
    await session.refresh(run)
    assert run.status == STATUS_FALLIDO  # nothing scored is a terminal state, not a limbo


async def test_start_chain_rejects_an_unknown_run(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await start_chain(str(uuid.uuid4()), session=session)


# --- advance_chain: the unit of work -----------------------------------------


async def test_advance_chain_scores_only_its_own_turn(
    session: AsyncSession, registry
) -> None:
    # The whole point of the granularity: one job judges one turn and stops.
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})
    turns = run.scenario_results[0].platform_executions[0].turns
    judge = RecordingJudge()

    first = await start_chain(str(run.id), session=session)
    nxt = await advance_chain(first.turn_id, session=session, judge=judge)

    assert [t.prompt for t in judge.seen_turns] == ["a"]
    assert turns[0].turn_score is not None
    assert turns[1].turn_score is None  # untouched
    assert nxt is not None and nxt.turn_id == str(turns[1].id)


async def test_advance_chain_retrieves_before_scoring_its_turn(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b")]}}, context=True)
    pipeline = _FakePipeline()
    judge = RecordingJudge()

    first = await start_chain(str(run.id), session=session)
    await advance_chain(first.turn_id, session=session, judge=judge, pipeline=pipeline)

    turn = run.scenario_results[0].platform_executions[0].turns[0]
    assert [
        [s["text"] for s in d.sentences] for d in turn.retrieved_documents
    ] == [["md::ref-claude/esc-1/1"]]
    # The judge saw the context the same job had just fetched.
    assert judge.seen_turns[0].retrieved_context.documents[0].content == (
        "md::ref-claude/esc-1/1"
    )


async def test_advance_chain_purges_the_cache_per_turn(
    session: AsyncSession, registry
) -> None:
    # Each job builds its own orchestrator, so a purge deferred to the platform boundary
    # would strand every earlier turn's bytes.
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}}, context=True)
    pipeline = _FakePipeline()

    await _drive(session, run, judge=RecordingJudge(), pipeline=pipeline)

    assert len(pipeline.purges) == 2  # once per turn, not once per platform


async def test_advance_chain_rebuilds_history_from_preceding_turns(
    session: AsyncSession, registry
) -> None:
    # History travels in the database, not in the message — including the unselected
    # middle turn, which is part of the conversation but is never scored.
    run = await _seed_run(
        session,
        {"claude": {"esc-1": [("a", "b"), ("medio", "resp"), ("c", "d")]}},
        selected={"claude/esc-1/1", "claude/esc-1/3"},
    )
    judge = RecordingJudge()

    await _drive(session, run, judge=judge)

    assert judge.seen_turns[0].history == []
    assert judge.seen_turns[1].history == [("a", "b"), ("medio", "resp")]


async def test_advance_chain_paces_the_successor(
    session: AsyncSession, registry, real_turn_pacing, monkeypatch
) -> None:
    # The pacing is handed back as a countdown rather than slept inside the job.
    monkeypatch.setattr("scorekeeper.core.runner.random.uniform", lambda low, high: 3.5)
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})

    first = await start_chain(str(run.id), session=session)
    nxt = await advance_chain(first.turn_id, session=session, judge=RecordingJudge())

    assert nxt is not None and nxt.countdown == 3.5


# --- ordering & boundaries ----------------------------------------------------


async def test_chain_walks_platforms_scenarios_and_turns_in_order(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(
        session,
        {
            "gemini": {"esc-2": [("g2a", "r"), ("g2b", "r")], "esc-1": [("g1", "r")]},
            "claude": {"esc-1": [("c1", "r")]},
        },
    )
    judge = RecordingJudge()

    await _drive(session, run, judge=judge)

    # platform, then scenario_id, then turn_number — never relationship order.
    assert [t.prompt for t in judge.seen_turns] == ["c1", "g1", "g2a", "g2b"]


async def test_scenario_rollup_happens_on_its_last_turn(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(
        session, {"claude": {"esc-1": [("a", "b"), ("c", "d")], "esc-2": [("e", "f")]}}
    )
    # The score lives on the execution; the scenario only rolls up a status.
    by_id = {s.scenario_id: s.platform_executions[0] for s in run.scenario_results}
    status_of = {s.scenario_id: s for s in run.scenario_results}

    first = await start_chain(str(run.id), session=session)
    second = await advance_chain(first.turn_id, session=session, judge=RecordingJudge())
    assert by_id["esc-1"].average_score is None  # mid-conversation: not rolled up yet

    third = await advance_chain(second.turn_id, session=session, judge=RecordingJudge())
    assert by_id["esc-1"].average_score == pytest.approx(0.8)
    assert status_of["esc-1"].status == STATUS_COMPLETADO
    assert by_id["esc-2"].average_score is None  # its own turn has not run

    await advance_chain(third.turn_id, session=session, judge=RecordingJudge())
    assert by_id["esc-2"].average_score == pytest.approx(0.8)


async def test_execution_rolls_up_before_its_scenario(
    session: AsyncSession, registry
) -> None:
    # One scenario run on two platforms — the shape the whole inversion exists for.
    # Each execution closes on its own last turn; the scenario closes over both only
    # once the second one is done.
    run = await _seed_run(
        session, {"claude": {"esc-1": [("a", "b")]}, "gemini": {"esc-1": [("c", "d")]}}
    )
    scenario = run.scenario_results[0]
    by_platform = {pe.platform: pe for pe in scenario.platform_executions}
    assert set(by_platform) == {"claude", "gemini"}  # one scenario, two conversations

    first = await start_chain(str(run.id), session=session)
    second = await advance_chain(first.turn_id, session=session, judge=RecordingJudge())

    # The first platform is finished; the scenario is not, since one platform is left.
    assert by_platform["claude"].average_score == pytest.approx(0.8)
    assert by_platform["claude"].finished_at is not None
    assert by_platform["gemini"].finished_at is None
    assert scenario.status != STATUS_COMPLETADO

    await advance_chain(second.turn_id, session=session, judge=RecordingJudge())
    assert by_platform["gemini"].finished_at is not None
    # The scenario rolls up a status over both platforms — never a blended average.
    assert scenario.status == STATUS_COMPLETADO
    assert [pe.average_score for pe in scenario.platform_executions] == [
        pytest.approx(0.8),
        pytest.approx(0.8),
    ]


async def test_platform_started_at_is_stamped_once(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})
    platform_exec = run.scenario_results[0].platform_executions[0]

    first = await start_chain(str(run.id), session=session)
    await advance_chain(first.turn_id, session=session, judge=RecordingJudge())
    stamped = platform_exec.started_at

    await advance_chain(
        str(platform_exec.turns[1].id),
        session=session,
        judge=RecordingJudge(),
    )
    assert platform_exec.started_at == stamped


async def test_run_rollup_happens_on_the_last_turn(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})

    await _drive(session, run, judge=RecordingJudge())

    await session.refresh(run)
    assert run.status == STATUS_COMPLETADO


async def test_scenario_without_selected_turns_is_finalized_at_the_end(
    session: AsyncSession, registry
) -> None:
    # The chain only walks selected turns, so an all-deselected scenario is never
    # visited; without the closing sweep it would keep "pending" for good and drag the
    # run status down with it.
    run = await _seed_run(
        session,
        {"claude": {"esc-1": [("a", "b")], "esc-2": [("c", "d")]}},
        selected={"claude/esc-1/1"},
    )

    await _drive(session, run, judge=RecordingJudge())

    by_id = {s.scenario_id: s for s in run.scenario_results}
    assert by_id["esc-2"].status == STATUS_FALLIDO  # nothing scored in it
    assert by_id["esc-2"].platform_executions[0].average_score is None
    await session.refresh(run)
    assert run.status != "pending"


# --- duplicate deliveries, attempts, failure ---------------------------------


async def test_already_scored_turn_still_returns_the_successor(
    session: AsyncSession, registry
) -> None:
    # A re-delivery must repair the chain rather than stall it.
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})
    turns = run.scenario_results[0].platform_executions[0].turns
    first = await start_chain(str(run.id), session=session)
    await advance_chain(first.turn_id, session=session, judge=RecordingJudge())

    judge = RecordingJudge()
    again = await advance_chain(first.turn_id, session=session, judge=judge)

    assert judge.seen_turns == []  # no re-judging
    assert turns[0].attempts == 1  # and no attempt burned
    assert again is not None and again.turn_id == str(turns[1].id)


async def test_advance_chain_returns_none_when_the_lock_is_held(
    session: AsyncSession, registry, monkeypatch
) -> None:
    # The live chain owns the successor; a duplicate that cannot get the lock drops out.
    from contextlib import asynccontextmanager

    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})
    first = await start_chain(str(run.id), session=session)

    @asynccontextmanager
    async def _busy(db, run_key):
        yield False

    monkeypatch.setattr(chain, "run_lock", _busy)
    judge = RecordingJudge()

    assert await advance_chain(first.turn_id, session=session, judge=judge) is None
    assert judge.seen_turns == []


async def test_advance_chain_abandons_a_turn_at_the_attempt_cap_and_keeps_going(
    session: AsyncSession, registry
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b"), ("c", "d")]}})
    turns = run.scenario_results[0].platform_executions[0].turns
    turns[0].attempts = MAX_TURN_ATTEMPTS
    await session.commit()
    judge = RecordingJudge()

    first = await start_chain(str(run.id), session=session)
    nxt = await advance_chain(first.turn_id, session=session, judge=judge)

    assert judge.seen_turns == []
    assert turns[0].turn_score is None
    assert nxt is not None and nxt.turn_id == str(turns[1].id)  # the chain moves past it


async def test_advance_chain_marks_the_run_fallido_on_a_hard_failure(
    session: AsyncSession, registry, monkeypatch
) -> None:
    run = await _seed_run(session, {"claude": {"esc-1": [("a", "b")]}})
    first = await start_chain(str(run.id), session=session)

    async def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(chain.selection, "resolve", _boom)

    with pytest.raises(RuntimeError):
        await advance_chain(first.turn_id, session=session, judge=RecordingJudge())

    reloaded = await session.get(BenchmarkRun, run.id)
    assert reloaded.status == STATUS_FALLIDO


async def test_advance_chain_rejects_an_unknown_turn(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await advance_chain(str(uuid.uuid4()), session=session)
