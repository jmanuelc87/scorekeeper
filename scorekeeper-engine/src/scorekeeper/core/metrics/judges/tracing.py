"""A ``Judge`` decorator that traces every LLM API call.

Every call a metric makes to a provider funnels through the ``Judge`` seam
(:class:`scorekeeper.core.metrics.judge.Judge`) — ``score``/``structured``/``embed`` on
the concrete judges are the only places that touch a real SDK client. Wrapping the
seam therefore traces *all* providers (Anthropic, OpenAI, LM Studio) uniformly,
without importing any SDK and without changing a single metric.

:class:`TracingJudge` wraps another judge, delegates every call to it, and emits a
:class:`LlmCallTrace` — the operation, resolved model, per-step role, turn number,
input/output sizes, latency, and outcome — to a pluggable ``sink``. The default
sink logs to ``scorekeeper.core.metrics.judges.tracing`` so tracing is on as soon as the
flag is set, with no extra wiring. It is deliberately *observational*: it never
alters arguments, results, or exceptions (errors are recorded, then re-raised), so
enabling it can only add log lines, never change scoring.

Enable it via ``Settings.judge_trace_enabled``; :func:`scorekeeper.core.metrics.judges.make_judge`
wraps whatever judge it built when the flag is on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog
from pydantic import BaseModel

from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict

if TYPE_CHECKING:
    from scorekeeper.core.metrics.base import TurnView
    from scorekeeper.core.metrics.judge import Judge
    from scorekeeper.core.metrics.scale import Scale

logger = structlog.get_logger("scorekeeper.core.metrics.judges.tracing")

T = TypeVar("T", bound=BaseModel)


class LlmCallTrace(BaseModel):
    """One traced LLM API call: what ran, on which model, how big, how long, outcome.

    JSON-serializable (pydantic) so a sink can log it as one structured line or
    persist it later. Sizes are character counts, not tokens — cheap to compute
    here and SDK-agnostic; token/cost accounting would need the judges to surface
    ``usage`` from the SDK response, which this observational decorator does not do.
    """

    op: str  # "score" | "structured" | "embed"
    model: str | None = None  # resolved model for the call (None when unresolvable)
    step: str | None = None  # JudgeStep role, when the caller labeled the call
    turn_number: int | None = None
    prompt_chars: int | None = None  # score/structured: size of the turn prompt
    response_chars: int | None = None  # score/structured: size of the turn response
    text_count: int | None = None  # embed: number of texts submitted
    latency_ms: float = 0.0
    ok: bool = True
    error: str | None = None  # repr of the raised exception when ok is False


def _log_sink(trace: LlmCallTrace) -> None:
    """Default sink: emit the trace as one structured structlog event.

    The trace's fields become event key/values, so they land as JSON keys in the
    telemetry output and ``key=value`` pairs on the console. ``None`` fields are
    dropped to keep each line to what actually applies to the call. Failures log at
    ``warning`` under a distinct ``llm_call_failed`` event.
    """
    fields = trace.model_dump(exclude_none=True)
    if trace.ok:
        logger.info("llm_call", **fields)
    else:
        logger.warning("llm_call_failed", **fields)


class TracingJudge:
    """Wrap a ``Judge`` to trace each call; structurally satisfies the Protocol.

    Delegates every method to ``inner`` and reports a :class:`LlmCallTrace` to
    ``sink`` (default: :func:`_log_sink`). ``model_for`` is pure resolution with no
    API call, so it is forwarded untraced.
    """

    def __init__(
        self,
        inner: Judge,
        *,
        sink: Callable[[LlmCallTrace], None] | None = None,
    ) -> None:
        self._inner = inner
        self._sink = sink or _log_sink

    def model_for(self, step: JudgeStep | None = None) -> str:
        return self._inner.model_for(step)

    def _resolved_model(self, step: JudgeStep | None, model: str | None) -> str | None:
        """Best-effort model for the trace: explicit wins, else step routing.

        Mirrors the judges' own precedence (explicit ``model`` over ``step``).
        Resolution can raise for a foreign model; a trace must never mask that
        error, so fall back to ``None`` and let the real call raise.
        """
        if model is not None:
            return model
        try:
            return self._inner.model_for(step)
        except Exception:
            return None

    def _run(self, trace: LlmCallTrace, call: Callable[[], Any]) -> Any:
        """Time ``call``, record the outcome on ``trace``, emit it, re-raise errors.

        The sink is invoked exactly once per call, whether it succeeds or raises.
        """
        start = time.perf_counter()
        try:
            result = call()
        except Exception as exc:
            trace.ok = False
            trace.error = repr(exc)
            raise
        else:
            trace.ok = True
            return result
        finally:
            trace.latency_ms = round((time.perf_counter() - start) * 1000, 2)
            self._sink(trace)

    def score(
        self,
        *,
        rubric: str,
        turn: TurnView,
        scale: Scale,
        rubric_version: str | None = None,
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> JudgeVerdict:
        trace = LlmCallTrace(
            op="score",
            model=self._resolved_model(step, model),
            step=step.value if step else None,
            turn_number=turn.turn_number,
            prompt_chars=len(turn.prompt),
            response_chars=len(turn.response),
        )
        return self._run(
            trace,
            lambda: self._inner.score(
                rubric=rubric,
                turn=turn,
                scale=scale,
                rubric_version=rubric_version,
                step=step,
                model=model,
            ),
        )

    def structured(
        self,
        *,
        instruction: str,
        turn: TurnView,
        schema: type[T],
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> T:
        trace = LlmCallTrace(
            op="structured",
            model=self._resolved_model(step, model),
            step=step.value if step else None,
            turn_number=turn.turn_number,
            prompt_chars=len(turn.prompt),
            response_chars=len(turn.response),
        )
        return self._run(
            trace,
            lambda: self._inner.structured(
                instruction=instruction,
                turn=turn,
                schema=schema,
                step=step,
                model=model,
            ),
        )

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        trace = LlmCallTrace(
            op="embed",
            # Embeddings run on their own model family; model_for resolves chat
            # models, so only an explicit embedding model is recorded here.
            model=model,
            step=JudgeStep.EMBED.value,
            text_count=len(texts),
        )
        return self._run(trace, lambda: self._inner.embed(texts=texts, model=model))
