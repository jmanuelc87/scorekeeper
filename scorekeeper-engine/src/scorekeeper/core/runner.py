"""The scoring runner — the orchestration that turns stored turns into scores.

Everything the runner needs already exists as isolated pieces: metric *selection*
(``metrics.selection.resolve``), metric *behavior* (``Metric.evaluate`` against a
``Judge``), and *roll-up* math (``metrics.rollup``). ``EvalRunner`` is the loop that
wires them together over the persisted hierarchy
``BenchmarkRun → ScenarioResult → PlatformExecution → Turn`` and writes the results
back: one ``MetricScore`` per metric per turn, the composite ``turn_score``, and the
``average_score`` of each platform execution. Nothing averages *across* platforms — a
scenario rolls up a ``status``, not a score.

Design:

* **Layered** — a public method at every level (``run_benchmark`` / ``run_scenario`` /
  ``run_execution`` / ``run_turn``) so a caller can score a whole run or a single turn.
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
It is also the unit of *delivery* — ``services.chain`` scores one turn per Celery job —
so a crash costs one turn rather than the whole run, and the ``attempts`` counter bounds
how often a turn that keeps crashing is retried.
The roll-ups commit at their own level as they are computed: the ``turn_score`` in
``run_turn``, the execution ``average_score`` / ``status`` in ``run_execution``, and
the scenario ``status`` in ``run_scenario``.

**Resumable at metric granularity.** Scoring re-enters — a crashed Celery worker
redelivers the whole job — and the judge bill is the expensive part, so a re-run
must not re-pay for work that still holds. Every ``MetricScore`` carries a
``scoring_key`` fingerprinting what produced it (see ``metrics.fingerprint``);
``run_turn`` reuses the rows whose keys still match and judges only the rest, and
``run_scenario`` skips a scenario already ``completado`` outright. A re-run over
unchanged inputs therefore issues zero judge calls.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.config.settings import get_settings
from scorekeeper.db.models import (
    BenchmarkRun,
    MetricScore,
    MetricTrace,
    PlatformExecution,
    PromptVersion,
    ScenarioResult,
    Turn,
    TurnTokenUsage,
    _now,
)
from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.fingerprint import scoring_key
from scorekeeper.core.metrics.judge import Judge, JudgeStep
from scorekeeper.core.metrics.judges import make_judge
from scorekeeper.core.metrics.judges.base import UsageAccumulator, collect_usage
from scorekeeper.core.metrics.rollup import execution_average, turn_score
from scorekeeper.core.metrics.selection import active_templates, metrics_for, resolve
from scorekeeper.core.retrieval.embed import Embedder, EmbedError
from scorekeeper.core.retrieved_context import Chunk, RetrievedContext
from scorekeeper.db.repositories import embeddings as embeddings_repo

logger = logging.getLogger(__name__)

# Spanish scenario status literals, matching the ``status`` string convention.
STATUS_COMPLETADO = "completado"
STATUS_PARCIAL = "parcial"
STATUS_FALLIDO = "fallido"

# The chat steps whose model routing can change a score (``judges.base.StepModels``),
# hashed into every ``scoring_key``. ``EMBED`` is absent on purpose: ``model_for``
# resolves the *chat* default for it, so including it would duplicate ``SCORE`` rather
# than name the embedding model, which the ``Judge`` seam does not expose.
_FINGERPRINT_STEPS = (JudgeStep.EXTRACT, JudgeStep.VERIFY, JudgeStep.SCORE)

# How many times a turn may be *started* before it is abandoned. A turn that kills its
# worker (the OOM killer) is redelivered by Celery, so without a cap it would block the
# run forever. Note the arithmetic differs by phase model: while retrieval and scoring
# are separate passes over the run, one healthy delivery burns two attempts (one each),
# so a poison turn is dropped on its second or third delivery.
MAX_TURN_ATTEMPTS = 3


def turn_delay_seconds() -> float:
    """How long to wait before the next turn, drawn from the configured window.

    Uniform over ``[turn_delay_min_seconds, turn_delay_max_seconds]``; ``0.0`` when the
    upper bound is non-positive, which disables pacing. One source of truth for the
    window: the in-process runner sleeps it (``EvalRunner._pace_between_turns``) while
    the chained per-turn jobs hand it to Celery as a ``countdown`` instead.
    """
    settings = get_settings()
    if settings.turn_delay_max_seconds <= 0:
        return 0.0
    low = max(0.0, settings.turn_delay_min_seconds)
    high = max(low, settings.turn_delay_max_seconds)
    return random.uniform(low, high)


class EvalRunner:
    """Scores stored turns with each scenario's selected metrics.

    Construct with an ``AsyncSession`` and, optionally, a ``Judge`` (defaults to the
    configured judge from ``make_judge()``). Tests inject a stub judge.

    ``embedder`` narrows what context a judge sees: the turn's prompt is embedded once
    and each document keeps only its ``embedding_top_k`` closest chunks. ``None`` — no
    embedder configured, or none passed — hands over every stored chunk instead.
    """

    def __init__(
        self,
        session: AsyncSession,
        judge: Judge | None = None,
        templates: Mapping[str, dict[str, PromptVersion]] | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.session = session
        self.judge = judge or make_judge()
        self.embedder = embedder
        # The prompt versions this run is pinned to, resolved once by ``score_run``.
        # Per-run immutable configuration, like ``judge`` — which is why it lives here
        # rather than being threaded through ``run_benchmark``: every level of the
        # runner is a public entry point, and a caller starting at ``run_scenario``
        # must get the same pinning.
        self.templates = templates

    # ---- Level 1: whole run -------------------------------------------------
    async def run_benchmark(self, run: BenchmarkRun) -> None:
        """Score every scenario in ``run`` and commit."""
        logger.info(
            "Run %s: puntuando %d escenario(s)", run.id, len(run.scenario_results)
        )
        for scenario in run.scenario_results:
            await self.run_scenario(scenario)
        await self.session.commit()
        logger.info("Run %s: todos los escenarios puntuados", run.id)

    # ---- Level 2: one scenario (across platforms) --------------------------
    async def run_scenario(self, scenario: ScenarioResult) -> None:
        """Score every platform execution of one scenario, roll it up, and commit.

        The scenario is the comparison unit: its platforms are scored back to back so
        their scores can be read side by side. It rolls up only a ``status`` — averaging
        across platforms would blend the systems being compared into one number.

        A scenario already ``completado`` returns immediately — see
        :meth:`run_execution` for what that shortcut costs.
        """
        if scenario.status == STATUS_COMPLETADO:
            logger.info(
                "Escenario %s ya completado: se omite la puntuación.",
                scenario.scenario_id,
            )
            return
        logger.info(
            "Escenario %s (use_case=%s): %d plataforma(s)",
            scenario.scenario_id,
            scenario.use_case.name,
            len(scenario.platform_executions),
        )
        for platform_exec in scenario.platform_executions:
            await self.run_execution(platform_exec)
        scenario.status = scenario_status(scenario)
        await self.session.commit()
        logger.info(
            "Escenario %s finalizado: estado=%s, por plataforma=%s",
            scenario.scenario_id,
            scenario.status,
            {pe.platform: pe.average_score for pe in scenario.platform_executions},
        )

    # ---- Level 3: one platform execution (conversation) --------------------
    async def run_execution(self, platform_exec: PlatformExecution) -> None:
        """Score every turn of one captured conversation, roll up its average, commit.

        Metric selection follows the *scenario's* use case (its ``use_case_metrics``
        links) — every platform of a scenario is judged by the same metrics, which is
        what makes their scores comparable. The running conversation ``history`` is fed
        forward so later turns see earlier exchanges. Consecutive turns are paced by a
        short random delay (see ``_pace_between_turns``), but only after a turn that
        actually reached the judge — a resumed turn costs no quota and so earns no pause.

        An execution already ``completado`` returns immediately: it has nothing left to
        score, and its ``average_score`` / ``status`` are already persisted. Note the
        cost of that shortcut — it *shadows* the per-metric fingerprint, so a
        ``completado`` execution will not re-score after a new prompt version is
        published or the judge model is swapped, even though its keys no longer match.
        The fingerprint remains the mechanism for ``parcial``/``fallido`` executions
        and for a run interrupted mid-flight.

        Individual metric scores are already committed as they are written (see
        ``run_turn``); this final commit persists the execution roll-up
        (``average_score`` / ``status``).
        """
        if platform_exec.status == STATUS_COMPLETADO:
            logger.info(
                "Plataforma %s ya completada: se omite la puntuación.",
                platform_exec.platform,
            )
            return
        scenario = platform_exec.scenario_result
        platform_exec.started_at = _now()
        templates = self.templates
        if templates is None:
            # A caller entering here directly rather than through ``score_run`` is not
            # pinned; resolve the map ``score_run`` would have resolved so the
            # fingerprint still sees which prompt versions scored this conversation. Not
            # cached onto ``self.templates``: it covers only *this* use case's metric
            # names, and a partial map makes ``Metric.__init__`` raise for the next one.
            templates = await active_templates(
                self.session, await metrics_for(self.session, scenario.use_case_id)
            )
        metrics = await resolve(self.session, scenario.use_case_id, templates)
        prompt_versions = _prompt_version_ids(templates)
        logger.info(
            "Plataforma %s (escenario=%s): %d turno(s), métricas=%s",
            platform_exec.platform,
            scenario.scenario_id,
            len(platform_exec.turns),
            [metric.name for metric in metrics],
        )
        history: list[tuple[str, str]] = []
        turn_scores: list[float | None] = []
        judged = False
        for turn in platform_exec.turns:
            # Only selected turns are scored, but every turn feeds the conversation
            # history so a later selected turn's judge sees the full exchange.
            if turn.is_selected:
                if judged:  # pace only after a turn that actually called the judge
                    await self._pace_between_turns()
                judged |= await self.run_turn(turn, metrics, history, prompt_versions)
                turn_scores.append(turn.turn_score)
            history.append((turn.prompt, turn.response))
        platform_exec.average_score = execution_average(turn_scores)
        platform_exec.status = execution_status(platform_exec)
        platform_exec.finished_at = _now()
        await self.session.commit()
        logger.info(
            "Plataforma %s finalizada: estado=%s, promedio=%s",
            platform_exec.platform,
            platform_exec.status,
            platform_exec.average_score,
        )

    async def _pace_between_turns(self) -> None:
        """Pause between consecutive turns to spread out judge calls.

        The interval comes from :func:`turn_delay_seconds`; ``0.0`` means pacing is off.
        """
        delay = turn_delay_seconds()
        if not delay:
            return
        logger.debug("Pausa de %.2fs antes del siguiente turno", delay)
        await asyncio.sleep(delay)

    # ---- Level 4: one turn --------------------------------------------------
    async def run_turn(
        self,
        turn: Turn,
        metrics: list[Metric],
        history: list[tuple[str, str]] | None = None,
        prompt_versions: Mapping[str, Mapping[str, str]] | None = None,
    ) -> bool:
        """Evaluate the metrics this turn is missing, persist them, roll up the turn.

        Resumable at metric granularity. Each declared metric's inputs — its rubric
        version, the prompt versions bound to its slots, the judge models its steps
        resolve to, and the whole ``TurnView`` — hash into a ``scoring_key``
        (``metrics.fingerprint``); a stored score carrying that key is *reused
        untouched* and costs nothing. Only the metrics with no matching row reach the
        judge, and ``turn_score`` is recomputed over the union. A row is removed when
        its key no longer matches, when the use case no longer declares its metric, or
        when it duplicates another row for the same metric. A legacy row with a NULL
        key never matches, so it re-scores once.

        The pending metrics evaluate concurrently — one worker thread per metric —
        since each is an independent judge call; their results are then written back
        serially on the event loop. A metric that raises is logged and skipped
        (skip-metric-continue), leaving no row, so the next pass retries it.

        Commits each ``MetricScore`` once the concurrent evaluation has gathered its
        results, then commits the ``turn_score`` roll-up. The turn is the atomic-write
        unit: an interruption mid-turn loses that turn's in-flight scores, but earlier
        turns stay committed.

        Counts an attempt (durably, before the first judge call) and returns without
        scoring once ``MAX_TURN_ATTEMPTS`` is reached, so a turn that keeps killing its
        worker is abandoned rather than retried forever. An abandoned turn keeps
        ``turn_score`` ``None``, which the scenario roll-up already reads as ``parcial``.
        Only a pass that reaches the judge counts: a turn resolved entirely from reused
        rows never burned quota, so it never burns an attempt either.

        Returns whether any judge call was issued, so ``run_scenario`` can skip pacing
        for a turn it fully reused.

        Two consequences worth knowing. ``Metric.weight``/``scale`` are *not* in the
        key — they are roll-up inputs, not judge inputs — so changing one does not
        propagate into ``turn_score`` until something else forces a re-score. And
        ``history`` is in the key, so editing one turn's text invalidates every later
        turn of that conversation; that is correct, since their judges saw it.
        """
        query_embedding = await self._query_embedding(turn)
        chunks = await embeddings_repo.chunks_for_turn(
            self.session,
            turn_id=turn.id,
            query_embedding=query_embedding,
            # Only meaningful with something to rank against; without it every chunk is
            # returned whatever ``k`` says, so settings are not consulted at all.
            k=get_settings().embedding_top_k if query_embedding is not None else 0,
        )
        view = self._to_turn_view(turn, history or [], chunks)
        # ``run_scenario`` passes the ids it already flattened; a per-turn caller
        # (``services.chain``) only pins them on the runner, so fall back to those —
        # otherwise the same turn would fingerprint differently on each path.
        versions = (
            prompt_versions
            if prompt_versions is not None
            else _prompt_version_ids(self.templates or {})
        )
        judge_models = {step.value: self.judge.model_for(step) for step in _FINGERPRINT_STEPS}

        keys: dict[str, str] = {}
        keep: dict[str, MetricScore] = {}
        pending: list[Metric] = []
        for metric in metrics:
            # Computed for every declared metric, not just the pending ones: the write
            # loop below indexes it by name, and ``_evaluate_metrics`` drops failures,
            # so results cannot be zipped positionally against ``pending``.
            keys[metric.name] = scoring_key(
                metric,
                view,
                prompt_versions=versions.get(metric.name, {}),
                judge_models=judge_models,
            )
            match = next(
                (
                    score
                    for score in turn.metric_scores
                    if score.metric_name == metric.name
                    and score.scoring_key == keys[metric.name]
                ),
                None,
            )
            if match is None:
                pending.append(metric)
            else:
                keep[metric.name] = match

        # Everything the diff did not elect to keep: a stale key, a metric the use case
        # no longer declares, or a second row for a metric that is kept. That last case
        # is why this compares identity rather than name — ``clear()`` used to heal
        # duplicates, and a leftover would be double-counted by the roll-up below.
        stale = [
            score for score in turn.metric_scores if keep.get(score.metric_name) is not score
        ]
        if not pending and not stale:
            logger.info(
                "Turno %s: sin cambios, se reutilizan %d puntuación(es)",
                turn.turn_number,
                len(keep),
            )
            if turn.turn_score is None and keep:
                # Every score committed but the roll-up did not — a crash landed between
                # the two. Rebuild it from the reused rows: nothing else ever will, and
                # the chain reads ``turn_score`` to decide a turn is done.
                turn.turn_score = turn_score(turn.metric_scores)
                await self.session.commit()
            return False

        if pending:
            if turn.attempts >= MAX_TURN_ATTEMPTS:
                logger.warning(
                    "Turno %s abandonado tras %d intento(s)",
                    turn.turn_number,
                    turn.attempts,
                )
                return False
            # Commit before judging: the count only bounds retries if it survives the
            # crash that caused the retry.
            turn.attempts += 1
            await self.session.commit()

        for score in stale:
            turn.metric_scores.remove(score)
        # Land the deletes (and their cascaded traces) before the judge calls: it keeps
        # a pending delete set from riding across minutes of LLM latency, and a crash
        # mid-judge then leaves the stale rows gone rather than resurrecting them.
        await self.session.flush()

        logger.info(
            "Turno %s: evaluando %d métrica(s) en paralelo, %d reutilizada(s)",
            turn.turn_number,
            len(pending),
            len(keep),
        )
        # One accumulator for the whole turn — every metric thread adds each judge
        # call's tokens to it (see _evaluate_metrics), so the snapshot is the turn total.
        usage = UsageAccumulator()
        for result in await self._evaluate_metrics(view, pending, turn.turn_number, usage):
            turn.metric_scores.append(
                MetricScore(
                    metric_name=result.metric_name,
                    score=result.raw_score,
                    trace=MetricTrace(steps=result.trace.model_dump()["steps"]),
                    judge_model=result.judge_model,
                    rubric_version=result.rubric_version,
                    scoring_key=keys[result.metric_name],
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
        if pending:  # nothing was judged when only stale rows were dropped
            self._record_turn_usage(turn, usage, accumulate=bool(keep))
        await self.session.commit()
        logger.info(
            "Turno %s puntuado: turn_score=%s (%d métrica(s), %d nueva(s))",
            turn.turn_number,
            turn.turn_score,
            len(turn.metric_scores),
            len(pending),
        )
        return bool(pending)

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
    def _record_turn_usage(turn: Turn, usage: UsageAccumulator, *, accumulate: bool) -> None:
        """Persist the turn's summed token usage as its 1:1 ``TurnTokenUsage`` row.

        Updates the existing row in place rather than delete-then-insert, keeping a
        single row per turn. The row should describe the tokens behind the scores
        *currently* stored on the turn, which is what ``accumulate`` selects: a partial
        resume (``True``) only re-paid for the metrics it re-scored, and the reused
        scores' tokens were really spent, so they stay on the ledger; a full re-score
        (``False``) replaces, because the old tokens bought scores that no longer exist
        and adding them would double-count discarded work. ``run_turn`` skips this call
        entirely when it judged nothing, so a zeroed snapshot can never erase a real
        record.

        One imprecision this cannot avoid without per-metric attribution: on a *mixed*
        turn the stale metric's old tokens ride along, over-counting — the safe
        direction for a cost ledger.
        """
        snapshot = usage.snapshot()
        if turn.token_usage is None:
            turn.token_usage = TurnTokenUsage(
                input_tokens=snapshot.input_tokens,
                output_tokens=snapshot.output_tokens,
            )
        elif accumulate:
            turn.token_usage.input_tokens += snapshot.input_tokens
            turn.token_usage.output_tokens += snapshot.output_tokens
        else:
            turn.token_usage.input_tokens = snapshot.input_tokens
            turn.token_usage.output_tokens = snapshot.output_tokens

    # ---- Helpers ------------------------------------------------------------
    async def _query_embedding(self, turn: Turn) -> list[float] | None:
        """Embed the turn's prompt, once, to rank its documents' chunks against it.

        ``None`` whenever ranking is impossible or pointless — no embedder, no retrieved
        documents — and also when embedding fails: losing the *narrowing* is survivable
        (every chunk is rendered instead), losing the *turn* is not.
        """
        if self.embedder is None or not turn.retrieved_documents:
            return None
        try:
            # The SDK call is blocking; keep it off the event loop.
            vectors = await asyncio.to_thread(self.embedder.embed, [turn.prompt])
        except EmbedError:
            logger.warning(
                "No se pudo embeber el prompt del turno %s; se usa todo el contexto",
                turn.turn_number,
                exc_info=True,
            )
            return None
        return vectors[0] if vectors else None

    def _to_turn_view(
        self,
        turn: Turn,
        history: list[tuple[str, str]],
        chunks: dict[uuid.UUID, list[Chunk]] | None = None,
    ) -> TurnView:
        """Project an ORM ``Turn`` into the SDK-free ``TurnView`` metrics consume.

        Documents keep their ``rank`` — the *platform's* retriever order, which
        ``contextual_precision`` scores — while ``chunks`` carries the narrowing done
        *within* each document by ``db.repositories.embeddings.chunks_for_turn``. Ranking
        documents by our own cosine instead would make that metric measure our retriever
        rather than the one under test.
        """
        chunks = chunks or {}
        return TurnView(
            prompt=turn.prompt,
            response=turn.response,
            turn_number=turn.turn_number,
            history=list(history),
            retrieved_context=RetrievedContext(
                documents=[
                    row.to_document(chunks.get(row.id))
                    for row in sorted(turn.retrieved_documents, key=lambda r: r.rank)
                ]
            ),
            expected_output=turn.expected_output or "",
        )


def _prompt_version_ids(
    templates: Mapping[str, dict[str, PromptVersion]],
) -> dict[str, dict[str, str]]:
    """Flatten pinned prompt versions to ``metric name -> slot slug -> version id``.

    The fingerprint lives in the ORM-free metrics package, so the ids reach it as
    strings rather than as ``PromptVersion`` rows.
    """
    return {
        name: {slug: str(version.id) for slug, version in slots.items()}
        for name, slots in templates.items()
    }


def execution_status(platform_exec: PlatformExecution) -> str:
    """Classify one scored conversation as complete, partial, or fully failed.

    ``fallido`` when no turn scored, ``parcial`` when at least one turn failed to
    score, otherwise ``completado``.
    """
    scores = [turn.turn_score for turn in platform_exec.turns]
    if not scores or all(score is None for score in scores):
        return STATUS_FALLIDO
    if any(score is None for score in scores):
        return STATUS_PARCIAL
    return STATUS_COMPLETADO


def rollup_status(statuses: list[str]) -> str:
    """Roll child statuses up to their parent's.

    ``fallido`` when there is nothing or every child failed, ``completado`` when they
    all completed, otherwise ``parcial``. Shared by the scenario (over its platform
    executions) and the run (over its scenarios).
    """
    if not statuses or all(status == STATUS_FALLIDO for status in statuses):
        return STATUS_FALLIDO
    if all(status == STATUS_COMPLETADO for status in statuses):
        return STATUS_COMPLETADO
    return STATUS_PARCIAL


def scenario_status(scenario: ScenarioResult) -> str:
    """Roll a scenario's platform executions up to its own status."""
    return rollup_status([pe.status for pe in scenario.platform_executions])
