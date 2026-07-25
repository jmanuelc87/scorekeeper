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
* **Metrics evaluate concurrently within a turn** — each metric's judge calls are
  awaited together (``asyncio.gather`` over ``asyncio.to_thread``), since they are
  independent I/O-bound HTTP requests against *blocking* LLM SDKs. Turns stay
  **sequential** (the running ``history`` is fed forward, so a turn depends on the ones
  before it), and all session writes stay on the event loop — only the ORM-free
  ``metric.evaluate`` calls are offloaded to worker threads.

Commit boundary: the **turn is the atomic unit of durability** — a turn's metric
judge calls all run before its scores are written, then ``run_turn`` commits each
``MetricScore`` followed by the ``turn_score`` roll-up. An interruption mid-turn
loses that turn's in-flight scores, but every earlier turn/scenario stays committed.
The roll-ups commit at their own level as they are computed: the ``turn_score`` in
``run_turn``, the scenario ``average_score`` / ``status`` in ``run_scenario``, the
platform ``average_score`` in ``run_platform``.
"""

from __future__ import annotations

import asyncio
import logging
import random

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.config import get_settings
from scorekeeper.database import (
    BenchmarkRun,
    MetricScore,
    MetricTrace,
    PlatformExecution,
    ScenarioResult,
    Turn,
    TurnTokenUsage,
    _now,
)
from scorekeeper.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.judges import make_judge
from scorekeeper.metrics.judges.base import UsageAccumulator, collect_usage
from scorekeeper.metrics.rollup import platform_average, scenario_average, turn_score
from scorekeeper.metrics.selection import resolve_scenario
from scorekeeper.retrieved_context import RetrievedContext

logger = logging.getLogger(__name__)

# Spanish scenario status literals, matching the ``status`` string convention.
STATUS_COMPLETADO = "completado"
STATUS_PARCIAL = "parcial"
STATUS_FALLIDO = "fallido"


class EvalRunner:
    """Scores stored turns with each scenario's selected metrics.

    Construct with an ``AsyncSession`` and, optionally, a ``Judge`` (defaults to the
    configured judge from ``make_judge()``). Tests inject a stub judge.
    """

    def __init__(self, session: AsyncSession, judge: Judge | None = None) -> None:
        self.session = session
        self.judge = judge or make_judge()
        settings = get_settings()
        self._turn_delay_min = settings.turn_delay_min_seconds
        self._turn_delay_max = settings.turn_delay_max_seconds

    # ---- Level 1: whole run -------------------------------------------------
    async def run_benchmark(self, run: BenchmarkRun) -> None:
        """Score every platform execution in ``run`` and commit."""
        logger.info(
            "Run %s: puntuando %d ejecución(es) de plataforma",
            run.id,
            len(run.platform_executions),
        )
        for platform_exec in run.platform_executions:
            await self.run_platform(platform_exec)
        await self.session.commit()
        logger.info("Run %s: todas las plataformas puntuadas", run.id)

    # ---- Level 2: one platform ---------------------------------------------
    async def run_platform(self, platform_exec: PlatformExecution) -> None:
        """Score every scenario for one platform and roll up its average."""
        logger.info(
            "Plataforma %s: puntuando %d escenario(s)",
            platform_exec.platform,
            len(platform_exec.scenario_results),
        )
        platform_exec.started_at = _now()
        scenario_scores: list[float | None] = []
        for scenario in platform_exec.scenario_results:
            await self.run_scenario(scenario)
            scenario_scores.append(scenario.average_score)
        platform_exec.average_score = platform_average(scenario_scores)
        platform_exec.finished_at = _now()
        await self.session.flush()
        logger.info(
            "Plataforma %s finalizada: promedio=%s",
            platform_exec.platform,
            platform_exec.average_score,
        )

    # ---- Level 3: one scenario (conversation) ------------------------------
    async def run_scenario(self, scenario: ScenarioResult) -> None:
        """Score every turn of one conversation, roll up its average, and commit.

        Metric selection follows ``scenario.use_case`` — a comma-separated list of
        tokens whose metrics are unioned; the running conversation ``history`` is
        fed forward so later turns see earlier exchanges. Consecutive turns are
        paced by a short random delay (see ``_pace_between_turns``).

        Individual metric scores are already committed as they are written (see
        ``run_turn``); this final commit persists the scenario roll-up
        (``average_score`` / ``status``).
        """
        metrics = await resolve_scenario(self.session, scenario.use_case)
        logger.info(
            "Escenario %s (use_case=%s): %d turno(s), métricas=%s",
            scenario.scenario_id,
            scenario.use_case,
            len(scenario.turns),
            [metric.name for metric in metrics],
        )
        history: list[tuple[str, str]] = []
        turn_scores: list[float | None] = []
        for index, turn in enumerate(scenario.turns):
            if index > 0:
                await self._pace_between_turns()
            await self.run_turn(turn, metrics, history)
            turn_scores.append(turn.turn_score)
            history.append((turn.prompt, turn.response))
        scenario.average_score = scenario_average(turn_scores)
        scenario.status = _scenario_status(scenario)
        await self.session.commit()
        logger.info(
            "Escenario %s finalizado: estado=%s, promedio=%s",
            scenario.scenario_id,
            scenario.status,
            scenario.average_score,
        )

    async def _pace_between_turns(self) -> None:
        """Pause a random interval between consecutive turns to spread out judge calls.

        The pause is drawn uniformly from the configured
        ``[turn_delay_min_seconds, turn_delay_max_seconds]`` window; a non-positive
        upper bound disables it.
        """
        if self._turn_delay_max <= 0:
            return
        low = max(0.0, self._turn_delay_min)
        high = max(low, self._turn_delay_max)
        delay = random.uniform(low, high)
        logger.debug("Pausa de %.2fs antes del siguiente turno", delay)
        await asyncio.sleep(delay)

    # ---- Level 4: one turn --------------------------------------------------
    async def run_turn(
        self,
        turn: Turn,
        metrics: list[Metric],
        history: list[tuple[str, str]] | None = None,
    ) -> None:
        """Evaluate every metric on one turn, persist scores, roll up the turn.

        The metrics evaluate concurrently — one worker thread per metric — since each is
        an independent judge call; their results are then written back serially on the
        event loop. A metric that raises is logged and skipped (skip-metric-continue); the
        turn scores from the survivors. Existing scores are cleared first so a re-run
        is idempotent (no duplicate ``MetricScore`` rows).

        Commits each ``MetricScore`` once the concurrent evaluation has gathered its
        results, then commits the ``turn_score`` roll-up. The turn is the atomic-write
        unit: an interruption mid-turn loses that turn's in-flight scores, but earlier
        turns stay committed.
        """
        view = self._to_turn_view(turn, history or [])
        turn.metric_scores.clear()
        logger.info(
            "Turno %s: evaluando %d métrica(s) en paralelo", turn.turn_number, len(metrics)
        )
        # One accumulator for the whole turn — every metric thread adds each judge
        # call's tokens to it (see _evaluate_metrics), so the snapshot is the turn total.
        usage = UsageAccumulator()
        for result in await self._evaluate_metrics(view, metrics, turn.turn_number, usage):
            turn.metric_scores.append(
                MetricScore(
                    metric_name=result.metric_name,
                    score=result.raw_score,
                    trace=MetricTrace(steps=result.trace.model_dump()["steps"]),
                    judge_model=result.judge_model,
                    rubric_version=result.rubric_version,
                )
            )
            await self.session.commit()  # persist each surviving metric's score
            logger.info(
                "Turno %s · métrica %s = %s",
                turn.turn_number,
                result.metric_name,
                result.raw_score,
            )
        turn.turn_score = turn_score(turn.metric_scores)
        self._record_turn_usage(turn, usage)
        await self.session.commit()
        logger.info(
            "Turno %s puntuado: turn_score=%s (%d métrica(s) exitosa(s))",
            turn.turn_number,
            turn.turn_score,
            len(turn.metric_scores),
        )

    async def _evaluate_metrics(
        self,
        view: TurnView,
        metrics: list[Metric],
        turn_number: int,
        usage: UsageAccumulator,
    ) -> list[MetricResult]:
        """Evaluate every metric concurrently — one worker thread each — and return the
        surviving results in metric-declaration order.

        Only ``metric.evaluate`` runs off the event loop: it takes the ORM-free ``view``
        and the shared, thread-safe ``judge``, touches no session state, and drives a
        *blocking* LLM SDK — hence ``to_thread`` rather than a bare await. Each worker
        activates the shared per-turn ``usage`` accumulator for the duration of its
        evaluate() and resets it on exit, so nothing leaks to the next task on a reused
        thread. ``gather`` yields results in *argument* order, which is what keeps the
        ``MetricScore`` rows in a stable order regardless of which judge call finishes
        first; ``return_exceptions`` is what keeps a failing metric from cancelling its
        siblings (skip-metric-continue) — it is logged and dropped.
        """
        if not metrics:
            return []

        def _evaluate(metric: Metric) -> MetricResult:
            with collect_usage(usage):
                return metric.evaluate(view, self.judge)

        outcomes = await asyncio.gather(
            *(asyncio.to_thread(_evaluate, metric) for metric in metrics),
            return_exceptions=True,
        )

        results: list[MetricResult] = []
        for metric, outcome in zip(metrics, outcomes):
            if isinstance(outcome, BaseException):  # skip-metric-continue
                logger.warning(
                    "Métrica %s falló en turno %s: %s",
                    metric.name,
                    turn_number,
                    outcome,
                )
                continue
            results.append(outcome)
        return results

    @staticmethod
    def _record_turn_usage(turn: Turn, usage: UsageAccumulator) -> None:
        """Persist the turn's summed token usage as its 1:1 ``TurnTokenUsage`` row.

        Updates the existing row in place when re-scoring (keeps a single row per
        turn, avoiding the unique-constraint churn of delete-then-insert).
        """
        snapshot = usage.snapshot()
        if turn.token_usage is None:
            turn.token_usage = TurnTokenUsage(
                input_tokens=snapshot.input_tokens,
                output_tokens=snapshot.output_tokens,
            )
        else:
            turn.token_usage.input_tokens = snapshot.input_tokens
            turn.token_usage.output_tokens = snapshot.output_tokens

    # ---- Helpers ------------------------------------------------------------
    def _to_turn_view(self, turn: Turn, history: list[tuple[str, str]]) -> TurnView:
        """Project an ORM ``Turn`` into the SDK-free ``TurnView`` metrics consume."""
        return TurnView(
            prompt=turn.prompt,
            response=turn.response,
            turn_number=turn.turn_number,
            history=list(history),
            retrieved_context=RetrievedContext(
                documents=[
                    row.to_document()
                    for row in sorted(turn.retrieved_documents, key=lambda r: r.rank)
                ]
            ),
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
