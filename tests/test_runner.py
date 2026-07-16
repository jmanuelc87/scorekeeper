"""Tests for the scoring runner — no live LLM, in-memory SQLite.

The runner is exercised through small fake metrics registered into an isolated
registry (rather than the real catalog metrics) so each behavior — happy path,
skip-on-error, roll-up, history wiring, idempotency — is asserted directly.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from scorekeeper.database import (
    Base,
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    ScenarioMetric,
    ScenarioResult,
    Turn,
)
from scorekeeper.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry
from scorekeeper.metrics.scale import Unit
from scorekeeper.runner import (
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

    def score(self, *, rubric, turn, scale, rubric_version=None) -> JudgeVerdict:
        self.seen_turns.append(turn)
        if self.fail_on_prompt is not None and turn.prompt == self.fail_on_prompt:
            raise RuntimeError("fallo del juez")
        return JudgeVerdict(score=self.score_value, justification="razón", model=self.model)

    def structured(self, *, instruction, turn, schema):  # pragma: no cover - unused here
        raise NotImplementedError

    def embed(self, *, texts):  # pragma: no cover - unused here
        raise NotImplementedError


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
            justification=verdict.justification,
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


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _select_metrics(session: Session, names: list[str], use_case: str = USE_CASE) -> None:
    for name in names:
        session.add(ScenarioMetric(use_case=use_case, metric_name=name))
    session.flush()


def _seed_scenario(
    session: Session,
    exchanges: list[tuple[str, str]],
    use_case: str = USE_CASE,
) -> tuple[BenchmarkRun, PlatformExecution, ScenarioResult]:
    run = BenchmarkRun()
    platform_exec = PlatformExecution(platform="claude", run=run)
    scenario = ScenarioResult(
        scenario_id="esc-1", use_case=use_case, platform_execution=platform_exec
    )
    for i, (prompt, response) in enumerate(exchanges, start=1):
        scenario.turns.append(Turn(turn_number=i, prompt=prompt, response=response))
    session.add(run)
    session.flush()
    return run, platform_exec, scenario


# --- Tests --------------------------------------------------------------------


def test_happy_path_scores_and_rolls_up(session: Session, registry) -> None:
    _select_metrics(session, ["utilidad", "correccion"])
    _, _, scenario = _seed_scenario(session, [("hola", "qué tal"), ("adiós", "hasta luego")])
    judge = RecordingJudge(score_value=0.8)

    EvalRunner(session, judge).run_scenario(scenario)
    session.commit()

    # Two metrics × two turns = four MetricScore rows.
    total = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()
    assert total == 4
    for turn in scenario.turns:
        assert turn.turn_score == pytest.approx(0.8)
        assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad", "correccion"}
        assert all(ms.judge_model == "judge-test" for ms in turn.metric_scores)
    assert scenario.average_score == pytest.approx(0.8)
    assert scenario.status == STATUS_COMPLETADO


def test_skip_on_error_keeps_survivor(session: Session, registry) -> None:
    _select_metrics(session, ["utilidad", "rota"])
    _, _, scenario = _seed_scenario(session, [("hola", "qué tal")])

    EvalRunner(session, RecordingJudge(score_value=0.6)).run_scenario(scenario)
    session.commit()

    turn = scenario.turns[0]
    # Only the surviving metric is persisted; the raised one is skipped.
    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad"}
    assert turn.turn_score == pytest.approx(0.6)
    # A turn that scored on some metric but lost others is still a scored turn,
    # so the scenario completed (no turn ended up unscored).
    assert scenario.status == STATUS_COMPLETADO


def test_all_metrics_fail_marks_scenario_fallido(session: Session, registry) -> None:
    _select_metrics(session, ["rota"])
    _, _, scenario = _seed_scenario(session, [("hola", "qué tal")])

    EvalRunner(session, RecordingJudge()).run_scenario(scenario)
    session.commit()

    turn = scenario.turns[0]
    assert turn.metric_scores == []
    assert turn.turn_score is None
    assert scenario.status == STATUS_FALLIDO


def test_partial_scenario_when_one_turn_unscored(session: Session, registry) -> None:
    # One metric applies to every turn; the judge fails only on turn 1's prompt,
    # so turn 1 goes unscored while turn 2 scores → the scenario is "parcial".
    _select_metrics(session, ["utilidad"])
    _, _, scenario = _seed_scenario(session, [("falla", "b"), ("bien", "d")])
    judge = RecordingJudge(score_value=0.7, fail_on_prompt="falla")

    EvalRunner(session, judge).run_scenario(scenario)
    session.commit()

    by_number = {t.turn_number: t.turn_score for t in scenario.turns}
    assert by_number[1] is None  # its only metric failed
    assert by_number[2] == pytest.approx(0.7)
    assert scenario.status == STATUS_PARCIAL
    # The scenario average ignores the unscored turn.
    assert scenario.average_score == pytest.approx(0.7)


def test_history_is_fed_forward(session: Session, registry) -> None:
    _select_metrics(session, ["utilidad"])
    _, _, scenario = _seed_scenario(session, [("hola", "respuesta1"), ("otra", "respuesta2")])
    judge = RecordingJudge()

    EvalRunner(session, judge).run_scenario(scenario)

    # One metric × two turns → two recorded views, in turn order.
    first_view, second_view = judge.seen_turns
    assert first_view.history == []
    assert second_view.history == [("hola", "respuesta1")]


def test_run_platform_rolls_up_average(session: Session, registry) -> None:
    _select_metrics(session, ["utilidad"])
    run = BenchmarkRun()
    platform_exec = PlatformExecution(platform="claude", run=run)
    for sid in ("s1", "s2"):
        scenario = ScenarioResult(
            scenario_id=sid, use_case=USE_CASE, platform_execution=platform_exec
        )
        scenario.turns.append(Turn(turn_number=1, prompt="p", response="r"))
    session.add(run)
    session.flush()

    EvalRunner(session, RecordingJudge(score_value=0.5)).run_platform(platform_exec)
    session.commit()

    assert platform_exec.average_score == pytest.approx(0.5)
    assert platform_exec.started_at is not None
    assert platform_exec.finished_at is not None


def test_rescoring_is_idempotent(session: Session, registry) -> None:
    _select_metrics(session, ["utilidad", "correccion"])
    _, _, scenario = _seed_scenario(session, [("hola", "qué tal")])
    runner = EvalRunner(session, RecordingJudge())

    runner.run_scenario(scenario)
    session.commit()
    count_1 = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()

    runner.run_scenario(scenario)
    session.commit()
    count_2 = session.execute(select(func.count()).select_from(MetricScore)).scalar_one()

    assert count_1 == count_2 == 2


def test_comma_separated_use_case_unions_metrics(session: Session, registry) -> None:
    # Two tokens, each selecting a different metric; the scenario scores the union.
    _select_metrics(session, ["utilidad"], use_case="utilidad_uc")
    _select_metrics(session, ["correccion"], use_case="correccion_uc")
    _, _, scenario = _seed_scenario(
        session, [("hola", "qué tal")], use_case="utilidad_uc, correccion_uc"
    )

    EvalRunner(session, RecordingJudge(score_value=0.9)).run_scenario(scenario)
    session.commit()

    turn = scenario.turns[0]
    assert {ms.metric_name for ms in turn.metric_scores} == {"utilidad", "correccion"}
    assert turn.turn_score == pytest.approx(0.9)
