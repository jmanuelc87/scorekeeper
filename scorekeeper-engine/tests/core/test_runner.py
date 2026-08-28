"""Tests for the scoring runner — no live LLM, in-memory SQLite.

The runner is exercised through small fake metrics registered into an isolated
registry (rather than the real catalog metrics) so each behavior — happy path,
skip-on-error, roll-up, history wiring, idempotency — is asserted directly.
"""

from __future__ import annotations

import threading
from typing import ClassVar, NamedTuple

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.repositories.runs import run_tree_options
from scorekeeper.db.models import (
    BenchmarkRun,
    JudgeCall,
    MetricDefinition,
    MetricScore,
    PlatformExecution,
    RetrievedContextDocument,
    RetrievedDocumentEmbedding,
    ScenarioResult,
    Turn,
    TurnTokenUsage,
    UseCase,
    UseCaseMetric,
)
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
from scorekeeper.core.metrics.judges.base import record_judge_call, record_usage
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.retrieval.embed import EmbedError
from scorekeeper.core.retrieved_context import Chunk
from scorekeeper.core.runner import (
    MAX_TURN_ATTEMPTS,
    STATUS_COMPLETADO,
    STATUS_FALLIDO,
    STATUS_PARCIAL,
    EvalRunner,
    turn_delay_seconds,
)

USE_CASE = "soporte"


# --- Fake judge & metrics -----------------------------------------------------


class RecordingJudge:
    """A Judge stub that returns a fixed verdict and records the turns it sees."""

    def __init__(
        self,
        score_value: float = 0.8,
        model: str = "judge-test",
        fail_on_prompt: str | None = None,
    ) -> None:
        self.score_value = score_value
        self.model = model
        # When set, scoring a turn with this prompt raises (simulates an API error).
        self.fail_on_prompt = fail_on_prompt
        self.seen_turns: list[TurnView] = []
        # Metrics within a turn now evaluate concurrently, so several threads may
        # call ``score`` at once — guard the record. (Turns stay sequential.)
        self._lock = threading.Lock()

    def model_for(self, step=None) -> str:
        return self.model

    def score(self, *, rubric, turn, scale, rubric_version=None, step=None) -> JudgeVerdict:
        with self._lock:
            self.seen_turns.append(turn)
        if self.fail_on_prompt is not None and turn.prompt == self.fail_on_prompt:
            raise RuntimeError("fallo del juez")
        return JudgeVerdict(score=self.score_value, justification="razón", model=self.model)

    def structured(self, *, instruction, turn, schema, step=None):  # pragma: no cover - unused here
        raise NotImplementedError

    def embed(self, *, texts):  # pragma: no cover - unused here
        raise NotImplementedError


class TokenRecordingJudge(RecordingJudge):
    """A ``RecordingJudge`` that also records fixed token usage on each ``score``.

    Simulates what a real concrete judge does (``record_usage`` from the SDK
    response's usage), so runner tests can assert the per-turn accumulation and
    persistence without any SDK fakes.
    """

    def __init__(self, *, input_tokens: int, output_tokens: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def score(self, *, rubric, turn, scale, rubric_version=None, step=None) -> JudgeVerdict:
        record_usage(input_tokens=self._input_tokens, output_tokens=self._output_tokens)
        return super().score(
            rubric=rubric, turn=turn, scale=scale, rubric_version=rubric_version, step=step
        )


class _JudgeMetric(Metric):
    """Base fake metric that scores via one ``judge.score`` call."""

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


class CallRecordingJudge(RecordingJudge):
    """A ``RecordingJudge`` that opens a ``record_judge_call`` around each ``score``.

    Simulates what a real concrete judge does around its model call, so runner tests
    can assert the persisted per-call prompt rows without any SDK fakes.
    """

    def score(self, *, rubric, turn, scale, rubric_version=None, step=None) -> JudgeVerdict:
        with record_judge_call(
            step=step, model=self.model, system_prompt="Sistema.", prompt=rubric
        ):
            record_usage(input_tokens=3, output_tokens=1)
            return super().score(
                rubric=rubric, turn=turn, scale=scale, rubric_version=rubric_version, step=step
            )


class Utilidad(_JudgeMetric):
    name = "utilidad"


class Correccion(_JudgeMetric):
    name = "correccion"


class DosLlamadas(Metric):
    """A metric that issues two judge calls, to exercise per-call ordering."""

    name = "doble"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:
        first = judge.score(rubric="Primera pregunta", turn=turn, scale=self.scale)
        judge.score(rubric="Segunda pregunta", turn=turn, scale=self.scale)
        return MetricResult(
            metric_name=self.name,
            raw_score=first.score,
            normalized_score=self.normalize(first.score),
            trace=MetricTrace(steps=[]),
            judge_model=first.model,
        )


class MetricaRota(Metric):
    """A metric that always fails, to exercise skip-metric-continue."""

    name = "rota"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:
        raise RuntimeError("fallo del juez")


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def registry():
    """Register the fake metrics into an isolated registry, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    for metric_cls in (Utilidad, Correccion, DosLlamadas, MetricaRota):
        MetricRegistry.add(metric_cls)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


async def _use_case(session: AsyncSession, name: str = USE_CASE) -> UseCase:
    """Get-or-create the ``use_cases`` row named ``name``."""
    row = (
        await session.execute(select(UseCase).where(UseCase.name == name))
    ).scalars().one_or_none()
    if row is None:
        row = UseCase(name=name)
        session.add(row)
        await session.flush()
    return row


async def _select_metrics(
    session: AsyncSession, names: list[str], use_case: str = USE_CASE
) -> None:
    row = await _use_case(session, use_case)
    for name in names:
        metric = MetricDefinition(name=name)
        session.add(metric)
        await session.flush()
        session.add(UseCaseMetric(use_case_id=row.id, metric_id=metric.id))
    await session.flush()


async def _seed_scenario(
    session: AsyncSession,
    exchanges: list[tuple[str, str]],
    use_case: str = USE_CASE,
    selected: set[int] | None = None,
) -> tuple[BenchmarkRun, PlatformExecution, ScenarioResult]:
    # ``selected`` is the set of 1-based turn numbers to flag for scoring; ``None``
    # selects every turn (the common case for tests that score the whole scenario).
    run = BenchmarkRun()
    scenario = ScenarioResult(
        scenario_id="esc-1",
        use_case=await _use_case(session, use_case),
        run=run,
    )
    platform_exec = PlatformExecution(platform="claude", scenario_result=scenario)
    for i, (prompt, response) in enumerate(exchanges, start=1):
        is_selected = selected is None or i in selected
        platform_exec.turns.append(
            Turn(turn_number=i, prompt=prompt, response=response, is_selected=is_selected)
        )
    session.add(run)
    await session.flush()
    # The runner consumes an eagerly-loaded tree (production loads it via
    # repositories.runs.run_tree_options); load it here too, or the first lazy relationship access
    # inside run_turn raises MissingGreenlet.
    await session.execute(
        select(BenchmarkRun)
        .where(BenchmarkRun.id == run.id)
        .options(run_tree_options())
        .execution_options(populate_existing=True)
    )
    return run, platform_exec, scenario


def _document(
    *, rank: float, name: str, document: str, text: str, url: str | None = None
) -> RetrievedContextDocument:
    """A retrieved document whose text lives on a single chunk row, as in production."""
    row = RetrievedContextDocument(rank=rank, name=name, document=document, url=url)
    row.embeddings = [
        RetrievedDocumentEmbedding(chunk_index=0, content=text, embedding=[1.0, 0.0])
    ]
    return row


# --- Tests --------------------------------------------------------------------


async def test_to_turn_view_rebuilds_context_from_child_rows(session: AsyncSession) -> None:
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "respuesta")])
    turn = platform_exec.turns[0]
    # Insert out of order to prove the ordered relationship sorts by rank.
    turn.retrieved_documents.append(_document(rank=1, name="b", document="d2.pdf", text="dos"))
    turn.retrieved_documents.append(
        _document(rank=0, name="a", document="d1.pdf", text="uno", url="http://x")
    )
    await session.flush()

    texts = {"d1.pdf": "uno", "d2.pdf": "dos"}
    chunks = {
        row.id: [Chunk(index=0, text=texts[row.document])]
        for row in turn.retrieved_documents
    }
    view = EvalRunner(session, RecordingJudge())._to_turn_view(turn, [], chunks)

    # Documents keep the platform's rank order; their text is whatever the chunk query
    # returned for each of them.
    assert [d.content for d in view.retrieved_context.documents] == ["uno", "dos"]
    assert view.retrieved_context.documents[0].url == "http://x"
    assert view.retrieved_context.node_texts() == ["d1.pdf\nuno", "d2.pdf\ndos"]


class _StubEmbedder:
    """Embeds any prompt as the same fixed direction, so ranking is predictable."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [self.vector]


async def test_to_turn_view_uses_the_chunks_it_is_handed(session: AsyncSession) -> None:
    """The narrowing itself runs in SQL; the runner only maps the result onto documents."""
    _, platform_exec, _ = await _seed_scenario(session, [("hola", "respuesta")])
    turn = platform_exec.turns[0]
    row = RetrievedContextDocument(rank=0, name="a", document="d.pdf")
    turn.retrieved_documents.append(row)
    await session.flush()

    view = EvalRunner(session, RecordingJudge())._to_turn_view(
        turn, [], {row.id: [Chunk(index=2, text="cerca")]}
    )

    assert view.retrieved_context.documents[0].content == "cerca"


async def test_to_turn_view_without_chunks_leaves_documents_textless(
    session: AsyncSession,
) -> None:
    _, platform_exec, _ = await _seed_scenario(session, [("hola", "respuesta")])
    turn = platform_exec.turns[0]
    turn.retrieved_documents.append(
        RetrievedContextDocument(rank=0, name="a", document="d.pdf")
    )
    await session.flush()

    view = EvalRunner(session, RecordingJudge())._to_turn_view(turn, [], {})

    assert view.retrieved_context.documents[0].content == ""


async def test_query_embedding_is_skipped_without_documents(session: AsyncSession) -> None:
    _, platform_exec, _ = await _seed_scenario(session, [("hola", "respuesta")])
    embedder = _StubEmbedder([1.0])
    runner = EvalRunner(session, RecordingJudge(), embedder=embedder)

    assert await runner._query_embedding(platform_exec.turns[0]) is None
    assert embedder.calls == 0  # nothing to rank, so nothing to pay for


async def test_a_failed_prompt_embedding_falls_back_to_the_whole_document(
    session: AsyncSession,
) -> None:
    _, platform_exec, _ = await _seed_scenario(session, [("hola", "respuesta")])
    turn = platform_exec.turns[0]
    turn.retrieved_documents.append(_document(rank=0, name="a", document="d.pdf", text="uno"))
    await session.flush()

    class _Failing:
        def embed(self, texts):
            raise EmbedError("sin cuota")

    runner = EvalRunner(session, RecordingJudge(), embedder=_Failing())
    # Losing the narrowing is survivable; losing the turn is not.
    assert await runner._query_embedding(turn) is None


async def test_to_turn_view_empty_context_when_no_child_rows(session: AsyncSession) -> None:
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "respuesta")])
    view = EvalRunner(session, RecordingJudge())._to_turn_view(platform_exec.turns[0], [])
    assert view.retrieved_context.is_empty


async def test_happy_path_scores_and_rolls_up(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal"), ("adiós", "hasta luego")])
    judge = RecordingJudge(score_value=0.8)

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # Two metrics × two turns = four MetricScore rows.
    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 4
    for turn in platform_exec.turns:
        assert turn.turn_score == pytest.approx(0.8)
        assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad", "correccion"}
        assert all(ms.judge_model == "judge-test" for ms in turn.metric_scores)
    assert platform_exec.average_score == pytest.approx(0.8)
    assert scenario.status == STATUS_COMPLETADO


async def test_skip_on_error_keeps_survivor(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "rota"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge(score_value=0.6)).run_scenario(scenario)
    await session.commit()

    turn = platform_exec.turns[0]
    # Only the surviving metric is persisted; the raised one is skipped.
    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad"}
    assert turn.turn_score == pytest.approx(0.6)
    # A turn that scored on some metric but lost others is still a scored turn,
    # so the scenario completed (no turn ended up unscored).
    assert scenario.status == STATUS_COMPLETADO


async def test_all_metrics_fail_marks_scenario_fallido(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["rota"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    turn = platform_exec.turns[0]
    assert turn.metric_scores == []
    assert turn.turn_score is None
    assert scenario.status == STATUS_FALLIDO


async def test_partial_scenario_when_one_turn_unscored(session: AsyncSession, registry) -> None:
    # One metric applies to every turn; the judge fails only on turn 1's prompt,
    # so turn 1 goes unscored while turn 2 scores → the scenario is "parcial".
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("falla", "b"), ("bien", "d")])
    judge = RecordingJudge(score_value=0.7, fail_on_prompt="falla")

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    by_number = {t.turn_number: t.turn_score for t in platform_exec.turns}
    assert by_number[1] is None  # its only metric failed
    assert by_number[2] == pytest.approx(0.7)
    assert scenario.status == STATUS_PARCIAL
    # The conversation's average ignores the unscored turn.
    assert platform_exec.average_score == pytest.approx(0.7)


async def test_only_selected_turns_are_scored(session: AsyncSession, registry) -> None:
    # Turn 2 is not selected: it must be skipped for scoring (no MetricScore rows,
    # turn_score stays None) while the selected turns are scored normally.
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(
        session,
        [("hola", "r1"), ("intermedio", "r2"), ("adiós", "r3")],
        selected={1, 3},
    )

    await EvalRunner(session, RecordingJudge(score_value=0.7)).run_scenario(scenario)
    await session.commit()

    by_number = {t.turn_number: t for t in platform_exec.turns}
    assert by_number[2].metric_scores == []
    assert by_number[2].turn_score is None
    assert by_number[1].turn_score == pytest.approx(0.7)
    assert by_number[3].turn_score == pytest.approx(0.7)
    # Only the two selected turns produced score rows.
    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 2
    # The skipped turn is excluded from the roll-up.
    assert platform_exec.average_score == pytest.approx(0.7)


async def test_unselected_turn_still_feeds_history(session: AsyncSession, registry) -> None:
    # Turn 2 is not scored, but turns 1 and 2 must still appear in turn 3's judge
    # history so the conversation the judge sees is complete.
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(
        session,
        [("hola", "r1"), ("intermedio", "r2"), ("adiós", "r3")],
        selected={1, 3},
    )
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)

    # Only the two selected turns were scored → two recorded views, in turn order.
    first_view, third_view = judge.seen_turns
    assert first_view.history == []
    assert third_view.history == [("hola", "r1"), ("intermedio", "r2")]


async def test_no_selected_turns_scores_nothing(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(
        session, [("hola", "r1"), ("adiós", "r2")], selected=set()
    )

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 0
    assert all(t.turn_score is None for t in platform_exec.turns)
    assert platform_exec.average_score is None


async def test_history_is_fed_forward(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "respuesta1"), ("otra", "respuesta2")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)

    # One metric × two turns → two recorded views, in turn order.
    first_view, second_view = judge.seen_turns
    assert first_view.history == []
    assert second_view.history == [("hola", "respuesta1")]


async def test_run_benchmark_scores_every_scenario(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    run = BenchmarkRun()
    use_case = await _use_case(session)
    for sid in ("s1", "s2"):
        scenario = ScenarioResult(scenario_id=sid, use_case=use_case, run=run)
        PlatformExecution(platform="claude", scenario_result=scenario).turns.append(
            Turn(turn_number=1, prompt="p", response="r", is_selected=True)
        )
    session.add(run)
    await session.flush()
    await session.execute(
        select(BenchmarkRun)
        .where(BenchmarkRun.id == run.id)
        .options(run_tree_options())
        .execution_options(populate_existing=True)
    )

    await EvalRunner(session, RecordingJudge(score_value=0.5)).run_benchmark(run)
    await session.commit()

    # Each execution rolls up from its turns; the scenario rolls up only a status,
    # since averaging across the platforms being compared would blend them.
    assert [s.status for s in run.scenario_results] == [STATUS_COMPLETADO] * 2
    assert not hasattr(run.scenario_results[0], "average_score")
    executions = [pe for s in run.scenario_results for pe in s.platform_executions]
    assert [pe.average_score for pe in executions] == [
        pytest.approx(0.5),
        pytest.approx(0.5),
    ]
    # Every conversation's scoring window is stamped around its own turns.
    assert all(
        pe.started_at is not None and pe.finished_at is not None for pe in executions
    )


async def test_rescoring_a_completed_scenario_is_skipped(
    session: AsyncSession, registry
) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()
    runner = EvalRunner(session, judge)

    await runner.run_scenario(scenario)
    await session.commit()
    count_1 = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    seen_1 = len(judge.seen_turns)

    await runner.run_scenario(scenario)
    await session.commit()
    count_2 = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()

    # No duplicate rows, and — the point of resumability — no second judge bill.
    assert count_1 == count_2 == 2
    assert len(judge.seen_turns) == seen_1
    assert platform_exec.turns[0].attempts == 1  # and no attempt burned re-visiting it


async def test_a_lost_turn_score_is_rebuilt_from_the_reused_scores(
    session: AsyncSession, registry
) -> None:
    # A crash between the last MetricScore commit and the roll-up commit. The keys still
    # match, so nothing is re-judged — but the turn must not stay unscored, since the
    # chain reads ``turn_score`` to decide a turn is done.
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()
    runner = EvalRunner(session, judge)

    await runner.run_scenario(scenario)
    await session.commit()
    scored = platform_exec.turns[0].turn_score
    judge.seen_turns.clear()

    _interrupt(scenario)
    platform_exec.turns[0].turn_score = None
    await runner.run_scenario(scenario)
    await session.commit()

    assert judge.seen_turns == []
    assert platform_exec.turns[0].turn_score == pytest.approx(scored)
    assert platform_exec.turns[0].attempts == 1  # rebuilt, not re-attempted


async def test_run_turn_counts_an_attempt(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)

    assert platform_exec.turns[0].attempts == 1


async def test_run_turn_abandons_a_turn_at_the_attempt_cap(
    session: AsyncSession, registry
) -> None:
    # A turn that keeps killing its worker is dropped rather than retried forever.
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    platform_exec.turns[0].attempts = MAX_TURN_ATTEMPTS
    await session.commit()
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)

    assert judge.seen_turns == []
    assert platform_exec.turns[0].turn_score is None
    assert platform_exec.turns[0].attempts == MAX_TURN_ATTEMPTS  # not bumped past the cap
    assert scenario.status == STATUS_FALLIDO  # nothing scored in this scenario


async def test_delay_paced_between_consecutive_turns(
    session: AsyncSession, registry, monkeypatch, real_turn_pacing
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d"), ("e", "f")])
    slept = real_turn_pacing
    monkeypatch.setattr("scorekeeper.core.runner.random.uniform", lambda low, high: 0.123)

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)

    # One pause between each consecutive pair of turns (N-1 for N turns), none after
    # the last turn.
    assert slept == [0.123, 0.123]


async def test_delay_disabled_when_max_non_positive(
    session: AsyncSession, registry, real_turn_pacing, monkeypatch
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d")])
    slept = real_turn_pacing
    monkeypatch.setattr(
        "scorekeeper.core.runner.get_settings",
        lambda: _DelaySettings(min_seconds=1.0, max_seconds=0.0),
    )

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)

    assert slept == []


class _DelaySettings(NamedTuple):
    """Just the two fields ``turn_delay_seconds`` reads off ``Settings``."""

    min_seconds: float
    max_seconds: float

    @property
    def turn_delay_min_seconds(self) -> float:
        return self.min_seconds

    @property
    def turn_delay_max_seconds(self) -> float:
        return self.max_seconds


def test_turn_delay_seconds_is_zero_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "scorekeeper.core.runner.get_settings",
        lambda: _DelaySettings(min_seconds=1.0, max_seconds=0.0),
    )
    assert turn_delay_seconds() == 0.0


def test_turn_delay_seconds_draws_from_the_configured_window(monkeypatch) -> None:
    monkeypatch.setattr(
        "scorekeeper.core.runner.get_settings",
        lambda: _DelaySettings(min_seconds=2.0, max_seconds=5.0),
    )
    monkeypatch.setattr(
        "scorekeeper.core.runner.random.uniform", lambda low, high: (low, high)
    )
    assert turn_delay_seconds() == (2.0, 5.0)


class _BarrierMetric(_JudgeMetric):
    """Metric whose evaluation blocks on a shared barrier.

    ``barrier.wait`` only releases once every sibling metric has reached it, so the
    turn completes iff all of them evaluate *at the same time* — i.e. on different
    threads. If they ran sequentially the first would time out, break the barrier,
    and every metric would fail (skip-metric-continue → no rows).
    """

    barrier: ClassVar[threading.Barrier]

    def evaluate(self, turn: TurnView, judge) -> MetricResult:
        self.barrier.wait(timeout=5)
        return super().evaluate(turn, judge)


async def test_metrics_evaluate_concurrently(session: AsyncSession, registry) -> None:
    names = ["m1", "m2", "m3"]
    barrier = threading.Barrier(len(names))
    for name in names:
        MetricRegistry.add(type(name, (_BarrierMetric,), {"name": name, "barrier": barrier}))
    await _select_metrics(session, names)
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge(score_value=0.9)).run_scenario(scenario)
    await session.commit()

    # All three rows exist only because the metrics ran on distinct threads and
    # cleared the barrier together; a sequential runner would time out and drop them.
    turn = platform_exec.turns[0]
    assert {ms.metric_name for ms in turn.metric_scores} == set(names)
    assert turn.turn_score == pytest.approx(0.9)


# --- Token usage persistence --------------------------------------------------


async def test_persists_summed_token_usage_per_turn(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal"), ("adiós", "chao")])
    judge = TokenRecordingJudge(input_tokens=10, output_tokens=4)

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # One row per turn; each turn ran 2 metrics × one score() call = 2 × (10, 4).
    assert (await session.execute(select(func.count()).select_from(TurnTokenUsage))).scalar_one() == 2
    for turn in platform_exec.turns:
        assert turn.token_usage is not None
        assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (20, 8)


async def test_token_usage_row_is_zero_when_judge_records_nothing(
    session: AsyncSession, registry
) -> None:
    # The plain RecordingJudge never calls record_usage, so the turn still gets a
    # row, summed to 0/0 (the accumulator with no adds).
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    turn = platform_exec.turns[0]
    assert turn.token_usage is not None
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (0, 0)


async def test_rescoring_updates_single_token_usage_row(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(
        session, TokenRecordingJudge(input_tokens=5, output_tokens=2)
    ).run_scenario(scenario)
    await session.commit()

    # A different judge model invalidates every key, so the second pass re-scores the
    # turn outright — the branch that *replaces* the counts rather than adding to them.
    _interrupt(scenario)
    await EvalRunner(
        session, TokenRecordingJudge(input_tokens=7, output_tokens=3, model="otro-juez")
    ).run_scenario(scenario)
    await session.commit()

    # Re-scoring updates the existing row in place — still exactly one per turn.
    assert (await session.execute(select(func.count()).select_from(TurnTokenUsage))).scalar_one() == 1
    turn = platform_exec.turns[0]
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (7, 3)


# --- Judge-call persistence ---------------------------------------------------


async def test_persists_one_row_per_judge_call_in_order(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["doble"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, CallRecordingJudge()).run_scenario(scenario)
    await session.commit()

    calls = (
        await session.execute(select(JudgeCall).order_by(JudgeCall.sequence))
    ).scalars().all()
    # The metric made two calls; ``sequence`` numbers them in call order, from 0.
    assert [c.sequence for c in calls] == [0, 1]
    assert [c.prompt for c in calls] == ["Primera pregunta", "Segunda pregunta"]
    (score,) = platform_exec.turns[0].metric_scores
    assert {c.metric_score_id for c in calls} == {score.id}
    assert all(c.system_prompt == "Sistema." for c in calls)
    assert all(c.model == "judge-test" for c in calls)
    # Each call carries the tokens its own response reported.
    assert [(c.input_tokens, c.output_tokens) for c in calls] == [(3, 1), (3, 1)]


async def test_rescoring_drops_the_previous_judge_calls(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, CallRecordingJudge()).run_scenario(scenario)
    await session.commit()

    # A different judge model invalidates the scoring key, so the stale MetricScore is
    # deleted and re-written — its calls go with it (CASCADE), leaving only the new one.
    _interrupt(scenario)
    await EvalRunner(session, CallRecordingJudge(model="otro-juez")).run_scenario(scenario)
    await session.commit()

    calls = (await session.execute(select(JudgeCall))).scalars().all()
    assert [(c.sequence, c.model) for c in calls] == [(0, "otro-juez")]


async def test_a_judge_that_records_nothing_writes_no_calls(
    session: AsyncSession, registry
) -> None:
    # The plain RecordingJudge never opens a record_judge_call, so the score is written
    # with an empty call list rather than failing.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    assert (await session.execute(select(func.count()).select_from(JudgeCall))).scalar_one() == 0


# --- Metric-level resume ------------------------------------------------------


def _interrupt(scenario: ScenarioResult) -> None:
    """Put a scored scenario back where a crashed worker would have left it.

    ``run_turn`` commits each score as it is written but ``run_execution`` only commits
    the roll-up at the end, so an interruption leaves the ``MetricScore`` rows on disk
    and the statuses untouched. Rewinding them reproduces that, and is also what lifts
    the ``completado`` shortcut so the per-metric fingerprint — the thing under test
    below — actually gets consulted. Both levels have that shortcut, so both are
    rewound: leaving the execution ``completado`` would skip it before the fingerprint
    is ever reached.
    """
    scenario.status = "pending"
    for platform_exec in scenario.platform_executions:
        platform_exec.status = "pending"


async def test_unchanged_turn_issues_no_judge_calls(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]
    before = {ms.metric_name: ms.id for ms in turn.metric_scores}
    score_before = turn.turn_score

    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # Same rows, not replacements: the keys matched, so nothing was judged again.
    assert {ms.metric_name: ms.id for ms in turn.metric_scores} == before
    assert len(judge.seen_turns) == 2  # two metrics, one turn, one pass
    assert turn.turn_score == score_before
    assert scenario.status == STATUS_COMPLETADO


@pytest.mark.parametrize("changed", ["response", "judge_model", "retrieved_context"])
async def test_changed_input_forces_a_rescore(
    session: AsyncSession, registry, changed: str
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]
    key_before = turn.metric_scores[0].scoring_key

    _interrupt(scenario)
    if changed == "response":
        turn.response = "una respuesta distinta"
    elif changed == "retrieved_context":
        turn.retrieved_documents.append(
            _document(rank=0, name="a", document="d.pdf", text="uno")
        )
    await session.flush()
    if changed == "judge_model":
        judge = RecordingJudge(model="otro-juez")

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # Re-judged, and still exactly one row for the metric — replaced, not appended.
    assert len(turn.metric_scores) == 1
    assert turn.metric_scores[0].scoring_key != key_before
    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 1


async def test_only_the_missing_metric_is_reevaluated(
    session: AsyncSession, registry
) -> None:
    # The motivating case: a metric that failed last pass is retried while the metric
    # that succeeded is reused, so the resumed run pays for one judge call, not two.
    class Intermitente(_JudgeMetric):
        name = "intermitente"
        falla = True

        def evaluate(self, turn: TurnView, judge) -> MetricResult:
            if type(self).falla:
                raise RuntimeError("fallo del juez")
            return super().evaluate(turn, judge)

    MetricRegistry.add(Intermitente)
    await _select_metrics(session, ["utilidad", "intermitente"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]
    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad"}
    utilidad_id = turn.metric_scores[0].id

    Intermitente.falla = False
    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad", "intermitente"}
    by_name = {ms.metric_name: ms for ms in turn.metric_scores}
    assert by_name["utilidad"].id == utilidad_id  # reused, not rewritten
    # One call on the first pass (utilidad; intermitente raised before reaching the
    # judge) plus one on the second (intermitente only).
    assert len(judge.seen_turns) == 2


async def test_legacy_null_scoring_key_is_rescored(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]
    # A row written before the column existed carries no provenance.
    turn.metric_scores[0].scoring_key = None
    await session.flush()

    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    assert len(judge.seen_turns) == 2  # NULL never matches, so it re-scored once
    assert turn.metric_scores[0].scoring_key is not None


async def test_row_for_an_undeclared_metric_is_dropped(
    session: AsyncSession, registry
) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge(score_value=0.8)

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]

    # Unlink "correccion" from the use case: its stored score no longer belongs to
    # the turn and must not keep feeding the roll-up.
    metric_id = (
        await session.execute(
            select(MetricDefinition.id).where(MetricDefinition.name == "correccion")
        )
    ).scalar_one()
    await session.execute(
        delete(UseCaseMetric).where(UseCaseMetric.metric_id == metric_id)
    )
    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad"}
    assert len(judge.seen_turns) == 2  # dropping a row costs no judge call
    assert turn.turn_score == pytest.approx(0.8)


async def test_duplicate_rows_for_one_metric_collapse(
    session: AsyncSession, registry
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]
    # Nothing in the schema prevents a duplicate; the diff has to heal it, since the
    # roll-up would otherwise count the metric twice.
    turn.metric_scores.append(
        MetricScore(
            metric_name="utilidad",
            score=0.1,
            scoring_key=turn.metric_scores[0].scoring_key,
        )
    )
    await session.flush()

    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 1
    assert len(judge.seen_turns) == 1  # the survivor's key still matched


async def test_partial_resume_accumulates_token_usage(
    session: AsyncSession, registry
) -> None:
    class Intermitente(_JudgeMetric):
        name = "intermitente"
        falla = True

        def evaluate(self, turn: TurnView, judge) -> MetricResult:
            if type(self).falla:
                raise RuntimeError("fallo del juez")
            return super().evaluate(turn, judge)

    MetricRegistry.add(Intermitente)
    await _select_metrics(session, ["utilidad", "intermitente"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = TokenRecordingJudge(input_tokens=5, output_tokens=2)

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()
    turn = platform_exec.turns[0]
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (5, 2)

    Intermitente.falla = False
    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # The reused metric's tokens were really spent, so the resumed pass adds to the
    # ledger instead of overwriting it — still one row per turn.
    assert (await session.execute(select(func.count()).select_from(TurnTokenUsage))).scalar_one() == 1
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (10, 4)


async def test_full_reuse_leaves_token_usage_untouched(
    session: AsyncSession, registry
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(
        session, TokenRecordingJudge(input_tokens=5, output_tokens=2)
    ).run_scenario(scenario)
    await session.commit()

    _interrupt(scenario)
    # Same model, so every key still matches; this judge must never be called, and a
    # zeroed snapshot must not overwrite what the first pass really spent.
    await EvalRunner(
        session, TokenRecordingJudge(input_tokens=999, output_tokens=999)
    ).run_scenario(scenario)
    await session.commit()

    turn = platform_exec.turns[0]
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (5, 2)


async def test_no_pacing_when_every_turn_is_reused(
    session: AsyncSession, registry, monkeypatch, real_turn_pacing
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, platform_exec, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d"), ("e", "f")])
    slept = real_turn_pacing
    monkeypatch.setattr("scorekeeper.core.runner.random.uniform", lambda low, high: 0.123)
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)
    assert slept == [0.123, 0.123]

    _interrupt(scenario)
    await EvalRunner(session, judge).run_scenario(scenario)

    # A turn that reached no judge earns no pause, so a fully-resumed scenario is
    # instant rather than sitting out the inter-turn delay for nothing.
    assert slept == [0.123, 0.123]
