"""The scoring runner — the orchestration that turns stored turns into scores.

Everything the runner needs already exists as isolated pieces: metric *selection*
(``metrics.selection.resolve``), metric *behavior* (``Metric.evaluate`` against a
``Judge``), and *roll-up* math (``metrics.rollup``). ``EvalRunner`` is the loop that
wires them together over the persisted hierarchy
``BenchmarkRun → PlatformExecution → ScenarioResult → Turn`` and writes the results
back: one ``MetricScore`` per metric per turn, plus the composite ``turn_score`` and
the ``average_score`` at scenario and platform level.

Design:

* **Layered** — a public method at every level (``run_benchmark`` / ``run_platform`` /
  ``run_scenario`` / ``run_turn``) so a caller can score a whole run or a single turn.
* **Skip-metric-continue** — a metric that raises is logged and skipped; the turn still
  scores from the metrics that succeeded, and scenario ``status`` records whether the
  scoring was complete, partial, or a total failure.
* **Synchronous** — a sequential loop, matching the fully-synchronous codebase.

Commit boundary: the inner methods only ``flush()``; the top-level method the caller
invokes commits. Callers entering at a lower level (e.g. ``run_scenario``) commit
themselves, mirroring how ``selection.sync_selection`` documents "caller commits".
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from scorekeeper.database import (
    BenchmarkRun,
    MetricScore,
    PlatformExecution,
    ScenarioResult,
    Turn,
    _now,
)
from scorekeeper.metrics.base import Metric, TurnView
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.judges import make_judge
from scorekeeper.metrics.rollup import platform_average, scenario_average, turn_score
from scorekeeper.metrics.selection import resolve_scenario

logger = logging.getLogger(__name__)

# Spanish scenario status literals, matching the ``status`` string convention.
STATUS_COMPLETADO = "completado"
STATUS_PARCIAL = "parcial"
STATUS_FALLIDO = "fallido"


class EvalRunner:
    """Scores stored turns with each scenario's selected metrics.

    Construct with a ``Session`` and, optionally, a ``Judge`` (defaults to the
    configured judge from ``make_judge()``). Tests inject a stub judge.
    """

    def __init__(self, session: Session, judge: Judge | None = None) -> None:
        self.session = session
        self.judge = judge or make_judge()

    # ---- Level 1: whole run -------------------------------------------------
    def run_benchmark(self, run: BenchmarkRun) -> None:
        """Score every platform execution in ``run`` and commit."""
        for platform_exec in run.platform_executions:
            self.run_platform(platform_exec)
        self.session.commit()

    # ---- Level 2: one platform ---------------------------------------------
    def run_platform(self, platform_exec: PlatformExecution) -> None:
        """Score every scenario for one platform and roll up its average."""
        platform_exec.started_at = _now()
        scenario_scores: list[float | None] = []
        for scenario in platform_exec.scenario_results:
            self.run_scenario(scenario)
            scenario_scores.append(scenario.average_score)
        platform_exec.average_score = platform_average(scenario_scores)
        platform_exec.finished_at = _now()
        self.session.flush()

    # ---- Level 3: one scenario (conversation) ------------------------------
    def run_scenario(self, scenario: ScenarioResult) -> None:
        """Score every turn of one conversation and roll up its average.

        Metric selection follows ``scenario.use_case`` — a comma-separated list of
        tokens whose metrics are unioned; the running conversation ``history`` is
        fed forward so later turns see earlier exchanges.
        """
        metrics = resolve_scenario(self.session, scenario.use_case)
        history: list[tuple[str, str]] = []
        turn_scores: list[float | None] = []
        for turn in scenario.turns:
            self.run_turn(turn, metrics, history)
            turn_scores.append(turn.turn_score)
            history.append((turn.prompt, turn.response))
        scenario.average_score = scenario_average(turn_scores)
        scenario.status = _scenario_status(scenario)
        self.session.flush()

    # ---- Level 4: one turn --------------------------------------------------
    def run_turn(
        self,
        turn: Turn,
        metrics: list[Metric],
        history: list[tuple[str, str]] | None = None,
    ) -> None:
        """Evaluate every metric on one turn, persist scores, roll up the turn.

        A metric that raises is logged and skipped (skip-metric-continue); the
        turn scores from the survivors. Existing scores are cleared first so a
        re-run is idempotent (no duplicate ``MetricScore`` rows).
        """
        view = self._to_turn_view(turn, history or [])
        turn.metric_scores.clear()
        for metric in metrics:
            try:
                result = metric.evaluate(view, self.judge)
            except Exception as exc:  # skip-metric-continue
                logger.warning(
                    "Métrica %s falló en turno %s: %s", metric.name, turn.turn_number, exc
                )
                continue
            turn.metric_scores.append(
                MetricScore(
                    metric_name=result.metric_name,
                    score=result.raw_score,
                    justification=result.justification,
                    judge_model=result.judge_model,
                    rubric_version=result.rubric_version,
                )
            )
        turn.turn_score = turn_score(turn.metric_scores)
        self.session.flush()

    # ---- Helpers ------------------------------------------------------------
    def _to_turn_view(self, turn: Turn, history: list[tuple[str, str]]) -> TurnView:
        """Project an ORM ``Turn`` into the SDK-free ``TurnView`` metrics consume."""
        return TurnView(
            prompt=turn.prompt,
            response=turn.response,
            turn_number=turn.turn_number,
            history=list(history),
            retrieved_context=turn.retrieved_context or "",
            expected_output=turn.expected_output or "",
        )


def _scenario_status(scenario: ScenarioResult) -> str:
    """Classify a scored scenario as complete, partial, or fully failed.

    ``fallido`` when no turn scored, ``parcial`` when at least one turn failed to
    score, otherwise ``completado``.
    """
    scores = [turn.turn_score for turn in scenario.turns]
    if not scores or all(score is None for score in scores):
        return STATUS_FALLIDO
    if any(score is None for score in scores):
        return STATUS_PARCIAL
    return STATUS_COMPLETADO
