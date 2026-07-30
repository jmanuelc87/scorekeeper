"""Tests for the scoring runner — no live LLM, in-memory SQLite.

The runner is exercised through small fake metrics registered into an isolated
registry (rather than the real catalog metrics) so each behavior — happy path,
skip-on-error, roll-up, history wiring, idempotency — is asserted directly.
"""

from __future__ import annotations

import threading
from typing import ClassVar

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.repositories.runs import run_tree_options
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricDefinition,
    MetricScore,
    PlatformExecution,
    RetrievedContextDocument,
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
from scorekeeper.core.metrics.judges.base import record_usage
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.runner import (
    MAX_TURN_ATTEMPTS,
    STATUS_COMPLETADO,
    STATUS_FALLIDO,
    STATUS_PARCIAL,
    EvalRunner,
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


class Utilidad(_JudgeMetric):
    name = "utilidad"


class Correccion(_JudgeMetric):
    name = "correccion"


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
    for metric_cls in (Utilidad, Correccion, MetricaRota):
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
    platform_exec = PlatformExecution(platform="claude", run=run)
    scenario = ScenarioResult(
        scenario_id="esc-1",
        use_case=await _use_case(session, use_case),
        platform_execution=platform_exec,
    )
    for i, (prompt, response) in enumerate(exchanges, start=1):
        is_selected = selected is None or i in selected
        scenario.turns.append(
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


# --- Tests --------------------------------------------------------------------


async def test_to_turn_view_rebuilds_context_from_child_rows(session: AsyncSession) -> None:
    _, _, scenario = await _seed_scenario(session, [("hola", "respuesta")])
    turn = scenario.turns[0]
    # Insert out of order to prove the ordered relationship sorts by rank.
    turn.retrieved_documents.append(
        RetrievedContextDocument(rank=1, name="b", document="d2.pdf", content="dos", url=None)
    )
    turn.retrieved_documents.append(
        RetrievedContextDocument(rank=0, name="a", document="d1.pdf", content="uno", url="http://x")
    )
    await session.flush()

    view = EvalRunner(session, RecordingJudge())._to_turn_view(turn, [])

    assert [d.content for d in view.retrieved_context.documents] == ["uno", "dos"]
    assert view.retrieved_context.documents[0].url == "http://x"
    assert view.retrieved_context.node_texts() == ["d1.pdf\nuno", "d2.pdf\ndos"]


async def test_to_turn_view_empty_context_when_no_child_rows(session: AsyncSession) -> None:
    _, _, scenario = await _seed_scenario(session, [("hola", "respuesta")])
    view = EvalRunner(session, RecordingJudge())._to_turn_view(scenario.turns[0], [])
    assert view.retrieved_context.is_empty


async def test_happy_path_scores_and_rolls_up(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal"), ("adiós", "hasta luego")])
    judge = RecordingJudge(score_value=0.8)

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # Two metrics × two turns = four MetricScore rows.
    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 4
    for turn in scenario.turns:
        assert turn.turn_score == pytest.approx(0.8)
        assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad", "correccion"}
        assert all(ms.judge_model == "judge-test" for ms in turn.metric_scores)
    assert scenario.average_score == pytest.approx(0.8)
    assert scenario.status == STATUS_COMPLETADO


async def test_skip_on_error_keeps_survivor(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "rota"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge(score_value=0.6)).run_scenario(scenario)
    await session.commit()

    turn = scenario.turns[0]
    # Only the surviving metric is persisted; the raised one is skipped.
    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad"}
    assert turn.turn_score == pytest.approx(0.6)
    # A turn that scored on some metric but lost others is still a scored turn,
    # so the scenario completed (no turn ended up unscored).
    assert scenario.status == STATUS_COMPLETADO


async def test_all_metrics_fail_marks_scenario_fallido(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["rota"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    turn = scenario.turns[0]
    assert turn.metric_scores == []
    assert turn.turn_score is None
    assert scenario.status == STATUS_FALLIDO


async def test_partial_scenario_when_one_turn_unscored(session: AsyncSession, registry) -> None:
    # One metric applies to every turn; the judge fails only on turn 1's prompt,
    # so turn 1 goes unscored while turn 2 scores → the scenario is "parcial".
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("falla", "b"), ("bien", "d")])
    judge = RecordingJudge(score_value=0.7, fail_on_prompt="falla")

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    by_number = {t.turn_number: t.turn_score for t in scenario.turns}
    assert by_number[1] is None  # its only metric failed
    assert by_number[2] == pytest.approx(0.7)
    assert scenario.status == STATUS_PARCIAL
    # The scenario average ignores the unscored turn.
    assert scenario.average_score == pytest.approx(0.7)


async def test_only_selected_turns_are_scored(session: AsyncSession, registry) -> None:
    # Turn 2 is not selected: it must be skipped for scoring (no MetricScore rows,
    # turn_score stays None) while the selected turns are scored normally.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(
        session,
        [("hola", "r1"), ("intermedio", "r2"), ("adiós", "r3")],
        selected={1, 3},
    )

    await EvalRunner(session, RecordingJudge(score_value=0.7)).run_scenario(scenario)
    await session.commit()

    by_number = {t.turn_number: t for t in scenario.turns}
    assert by_number[2].metric_scores == []
    assert by_number[2].turn_score is None
    assert by_number[1].turn_score == pytest.approx(0.7)
    assert by_number[3].turn_score == pytest.approx(0.7)
    # Only the two selected turns produced score rows.
    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 2
    # The skipped turn is excluded from the roll-up.
    assert scenario.average_score == pytest.approx(0.7)


async def test_unselected_turn_still_feeds_history(session: AsyncSession, registry) -> None:
    # Turn 2 is not scored, but turns 1 and 2 must still appear in turn 3's judge
    # history so the conversation the judge sees is complete.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(
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
    _, _, scenario = await _seed_scenario(
        session, [("hola", "r1"), ("adiós", "r2")], selected=set()
    )

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    total = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    assert total == 0
    assert all(t.turn_score is None for t in scenario.turns)
    assert scenario.average_score is None


async def test_history_is_fed_forward(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "respuesta1"), ("otra", "respuesta2")])
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)

    # One metric × two turns → two recorded views, in turn order.
    first_view, second_view = judge.seen_turns
    assert first_view.history == []
    assert second_view.history == [("hola", "respuesta1")]


async def test_run_platform_rolls_up_average(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    run = BenchmarkRun()
    platform_exec = PlatformExecution(platform="claude", run=run)
    use_case = await _use_case(session)
    for sid in ("s1", "s2"):
        scenario = ScenarioResult(
            scenario_id=sid, use_case=use_case, platform_execution=platform_exec
        )
        scenario.turns.append(
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

    await EvalRunner(session, RecordingJudge(score_value=0.5)).run_platform(platform_exec)
    await session.commit()

    assert platform_exec.average_score == pytest.approx(0.5)
    assert platform_exec.started_at is not None
    assert platform_exec.finished_at is not None


async def test_rescoring_skips_already_scored_turns(session: AsyncSession, registry) -> None:
    # Resume-forward: the second pass is a no-op, not a re-score. This is what makes a
    # re-delivered job cost at most the turn that was in flight.
    await _select_metrics(session, ["utilidad", "correccion"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()
    runner = EvalRunner(session, judge)

    await runner.run_scenario(scenario)
    await session.commit()
    count_1 = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()
    judge.seen_turns.clear()

    await runner.run_scenario(scenario)
    await session.commit()
    count_2 = (await session.execute(select(func.count()).select_from(MetricScore))).scalar_one()

    assert judge.seen_turns == []  # no judge call at all on the second pass
    assert count_1 == count_2 == 2
    turn = scenario.turns[0]
    assert turn.attempts == 1  # and no attempt burned re-visiting it


async def test_clearing_turn_score_rescores(session: AsyncSession, registry) -> None:
    # The escape hatch behind resume-forward: dropping the score is what re-opens a turn.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    judge = RecordingJudge()
    runner = EvalRunner(session, judge)

    await runner.run_scenario(scenario)
    await session.commit()
    judge.seen_turns.clear()

    scenario.turns[0].turn_score = None
    await runner.run_scenario(scenario)
    await session.commit()

    assert [turn.prompt for turn in judge.seen_turns] == ["hola"]
    assert scenario.turns[0].attempts == 2


async def test_scenario_average_includes_previously_scored_turns(
    session: AsyncSession, registry
) -> None:
    # A skipped turn must still weigh on the roll-up, or resuming would silently drop it.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d")])
    scenario.turns[0].turn_score = 0.2
    await session.commit()

    await EvalRunner(session, RecordingJudge(score_value=0.8)).run_scenario(scenario)

    assert scenario.turns[0].turn_score == pytest.approx(0.2)  # untouched
    assert scenario.average_score == pytest.approx(0.5)  # mean of 0.2 and 0.8


async def test_run_turn_counts_an_attempt(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)

    assert scenario.turns[0].attempts == 1


async def test_run_turn_abandons_a_turn_at_the_attempt_cap(
    session: AsyncSession, registry
) -> None:
    # A turn that keeps killing its worker is dropped rather than retried forever.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    scenario.turns[0].attempts = MAX_TURN_ATTEMPTS
    await session.commit()
    judge = RecordingJudge()

    await EvalRunner(session, judge).run_scenario(scenario)

    assert judge.seen_turns == []
    assert scenario.turns[0].turn_score is None
    assert scenario.turns[0].attempts == MAX_TURN_ATTEMPTS  # not bumped past the cap
    assert scenario.status == STATUS_FALLIDO  # nothing scored in this scenario


async def test_no_pause_before_the_first_turn_of_a_resumed_scenario(
    session: AsyncSession, registry, real_turn_pacing
) -> None:
    # Pacing spreads out judge calls, so it must follow one — a resumed scenario whose
    # first turns are skipped has made no call to spread out.
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d")])
    scenario.turns[0].turn_score = 0.5
    await session.commit()
    slept = real_turn_pacing

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)

    assert slept == []


async def test_delay_paced_between_consecutive_turns(
    session: AsyncSession, registry, monkeypatch, real_turn_pacing
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d"), ("e", "f")])
    slept = real_turn_pacing
    monkeypatch.setattr("scorekeeper.core.runner.random.uniform", lambda low, high: 0.123)

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)

    # One pause between each consecutive pair of turns (N-1 for N turns), none after
    # the last turn.
    assert slept == [0.123, 0.123]


async def test_delay_disabled_when_max_non_positive(
    session: AsyncSession, registry, real_turn_pacing
) -> None:
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("a", "b"), ("c", "d")])
    slept = real_turn_pacing

    runner = EvalRunner(session, RecordingJudge())
    runner._turn_delay_max = 0.0
    await runner.run_scenario(scenario)

    assert slept == []


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
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge(score_value=0.9)).run_scenario(scenario)
    await session.commit()

    # All three rows exist only because the metrics ran on distinct threads and
    # cleared the barrier together; a sequential runner would time out and drop them.
    turn = scenario.turns[0]
    assert {ms.metric_name for ms in turn.metric_scores} == set(names)
    assert turn.turn_score == pytest.approx(0.9)


# --- Token usage persistence --------------------------------------------------


async def test_persists_summed_token_usage_per_turn(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad", "correccion"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal"), ("adiós", "chao")])
    judge = TokenRecordingJudge(input_tokens=10, output_tokens=4)

    await EvalRunner(session, judge).run_scenario(scenario)
    await session.commit()

    # One row per turn; each turn ran 2 metrics × one score() call = 2 × (10, 4).
    assert (await session.execute(select(func.count()).select_from(TurnTokenUsage))).scalar_one() == 2
    for turn in scenario.turns:
        assert turn.token_usage is not None
        assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (20, 8)


async def test_token_usage_row_is_zero_when_judge_records_nothing(
    session: AsyncSession, registry
) -> None:
    # The plain RecordingJudge never calls record_usage, so the turn still gets a
    # row, summed to 0/0 (the accumulator with no adds).
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])

    await EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    await session.commit()

    turn = scenario.turns[0]
    assert turn.token_usage is not None
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (0, 0)


async def test_rescoring_updates_single_token_usage_row(session: AsyncSession, registry) -> None:
    await _select_metrics(session, ["utilidad"])
    _, _, scenario = await _seed_scenario(session, [("hola", "qué tal")])
    runner = EvalRunner(session, TokenRecordingJudge(input_tokens=5, output_tokens=2))

    await runner.run_scenario(scenario)
    await session.commit()
    await runner.run_scenario(scenario)
    await session.commit()

    # Re-scoring updates the existing row in place — still exactly one per turn.
    assert (await session.execute(select(func.count()).select_from(TurnTokenUsage))).scalar_one() == 1
    turn = scenario.turns[0]
    assert (turn.token_usage.input_tokens, turn.token_usage.output_tokens) == (5, 2)
