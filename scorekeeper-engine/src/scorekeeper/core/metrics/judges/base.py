"""SDK-free helpers shared by the concrete judges.

Everything an LLM judge needs *around* the model call — the Spanish system
prompt, turning a ``Scale`` into a Spanish range instruction, rendering a rubric
template plus the turn context, and clamping the model's score back into the
scale's range — lives here so it is unit-testable without any LLM SDK and so the
Anthropic and OpenAI judges stay thin wrappers over their respective clients.
"""

from __future__ import annotations

import contextvars
import logging
import random
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from typing import NamedTuple, TypeVar

from pydantic import BaseModel

from scorekeeper.config.settings import get_settings
from scorekeeper.core.metrics.base import TurnView
from scorekeeper.core.metrics.judge import JudgeStep
from scorekeeper.core.metrics.scale import Boolean, Likert, Scale, Unit

logger = logging.getLogger(__name__)

# Default system prompt for every judge call. Overridable per judge (constructor
# arg) or globally (``Settings.judge_system_prompt``). All scoring output is Spanish.
DEFAULT_SYSTEM_PROMPT = (
    "Eres un evaluador experto de conversaciones entre usuarios y asistentes de "
    "IA. Evalúas un único turno según una rúbrica y devuelves una puntuación "
    "numérica junto con una justificación breve. Sé objetivo y responde siempre "
    "en español."
)


# --- Token-usage collection ---------------------------------------------------
# Every judge call the SDKs make returns token counts, but the return types on the
# ``Judge`` seam (``JudgeVerdict`` / a bare schema / bare vectors) have nowhere to
# carry them. Rather than change the Protocol and every metric/stub, the concrete
# judges push each call's usage into an *ambient* accumulator via ``record_usage``.
# A caller (the runner) activates an accumulator for a scope with ``collect_usage``;
# outside any scope ``record_usage`` is a no-op, so judges stay usable standalone.


class TokenUsage(BaseModel):
    """Provider-neutral token counts for one or more LLM calls.

    Anthropic reports ``input_tokens``/``output_tokens``; OpenAI reports
    ``prompt_tokens``/``completion_tokens`` — both are normalized to input/output
    here. ``total_tokens`` is derived, never stored.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class UsageAccumulator:
    """A thread-safe running sum of :class:`TokenUsage`.

    One accumulator is shared by all the metric-evaluation threads of a single turn
    (see ``scorekeeper.core.runner``); each judge call adds to it under a lock, so the
    snapshot is the turn's total across every concurrent metric and every call each
    metric made.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage = TokenUsage()

    def add(self, *, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self._usage = self._usage + TokenUsage(
                input_tokens=input_tokens, output_tokens=output_tokens
            )

    def snapshot(self) -> TokenUsage:
        with self._lock:
            return self._usage.model_copy()


# The accumulator active on the current context, if any. Default None → recording is
# a no-op. A ``ContextVar`` (not a plain global) so concurrent turns/threads can each
# scope their own accumulator. The runner sets it *inside* each worker (see
# ``collect_usage``): ``asyncio.to_thread`` gives the worker a copy of the caller's
# context, so setting it there scopes the accumulator to that one evaluate() and cannot
# leak back to the caller or across to a sibling metric.
_usage_var: contextvars.ContextVar[UsageAccumulator | None] = contextvars.ContextVar(
    "scorekeeper_usage_accumulator", default=None
)


def record_usage(*, input_tokens: int | None, output_tokens: int | None) -> None:
    """Add one call's tokens to the active accumulator; no-op when none is active.

    ``None`` counts are treated as 0 so a provider (or a test fake) that omits
    ``usage`` contributes nothing rather than raising.
    """
    accumulator = _usage_var.get()
    if accumulator is None:
        return
    accumulator.add(input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0))


@contextmanager
def collect_usage(accumulator: UsageAccumulator) -> Generator[UsageAccumulator]:
    """Activate ``accumulator`` for judge calls made inside the ``with`` block.

    Entered inside the thread that makes the judge calls, so each metric's evaluate()
    scopes its own activation. Resets the context var on exit so the accumulator does
    not leak to later work scheduled on a reused worker thread.
    """
    token = _usage_var.set(accumulator)
    try:
        yield accumulator
    finally:
        _usage_var.reset(token)


class StepModels:
    """Resolve a :class:`JudgeStep` to the model name a judge should use for it.

    Wraps a ``default`` model plus optional per-step overrides. Unmapped steps —
    and a ``None`` step — resolve to the default, so a judge built with no
    overrides is indistinguishable from a plain single-model judge. Falsy override
    values (``None``/``""``) are ignored, letting callers pass through unset
    settings without special-casing.
    """

    def __init__(
        self,
        default: str,
        overrides: Mapping[JudgeStep | str, str | None] | None = None,
    ) -> None:
        self.default = default
        self._overrides: dict[JudgeStep, str] = {}
        for step, model in (overrides or {}).items():
            if model:
                self._overrides[JudgeStep(step)] = model

    def for_step(self, step: JudgeStep | str | None) -> str:
        """Return the model for ``step``, falling back to the default model."""
        if step is None:
            return self.default
        return self._overrides.get(JudgeStep(step), self.default)


T = TypeVar("T")


class JudgeError(RuntimeError):
    """A judge's model call failed in a way worth surfacing with full context.

    Carries the provider, model, and the step being run so the message points at
    the exact failing call instead of a bare SDK ``AttributeError``/transport error.
    The message is Spanish, like every other user-facing error in the pipeline.
    """


# --- Retry on provider throttling ---------------------------------------------
# The between-turn pause (``runner.turn_delay_seconds``) is open-loop and per-process:
# it cannot bound the aggregate request rate once a run is spread across Celery workers,
# and within one turn every metric fans out concurrently and each may issue many calls.
# The only pacing that survives that is *reactive* — each caller backs off from the
# throttling it personally sees, which needs no shared state. Full jitter is what keeps
# concurrent callers from re-firing in lockstep.

# Statuses that mean "capacity, come back later": 429 rate limit, 503 service
# unavailable, 529 Anthropic ``overloaded_error``. Everything else (auth, 400 for a bad
# parameter, a schema refusal) is a permanent failure that a retry only makes slower.
RETRYABLE_STATUSES = frozenset({429, 503, 529})


class RetryPolicy(NamedTuple):
    """How many times a throttled judge call retries, and how long it waits."""

    max_attempts: int  # counts the first try; 1 disables retrying
    base_seconds: float
    max_seconds: float


def retry_policy() -> RetryPolicy:
    """The configured backoff policy — one source of truth, like ``turn_delay_seconds``."""
    settings = get_settings()
    return RetryPolicy(
        max_attempts=max(1, settings.judge_retry_max_attempts),
        base_seconds=max(0.0, settings.judge_retry_base_seconds),
        max_seconds=max(0.0, settings.judge_retry_max_seconds),
    )


def _is_capacity_error(exc: Exception) -> bool:
    """Whether ``exc`` is a provider capacity failure worth retrying.

    Duck-typed on ``status_code`` rather than on SDK exception classes: both
    ``anthropic.APIStatusError`` and ``openai.APIStatusError`` expose it, and this module
    stays SDK-free (importable without the optional extras). Classifying by status and
    not by message text also means an unrelated error that merely *mentions* a rate limit
    is still treated as permanent.
    """
    return getattr(exc, "status_code", None) in RETRYABLE_STATUSES


def _retry_after_seconds(exc: Exception) -> float | None:
    """The provider's ``Retry-After`` hint in seconds, or ``None`` when it sent none.

    ``getattr``-safe the whole way down: an exception without a ``response``, without
    headers, or with a date-form (rather than seconds-form) value yields ``None`` and the
    caller falls back to computed backoff.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        seconds = float(headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _backoff_seconds(attempt: int, policy: RetryPolicy, exc: Exception) -> float:
    """How long to wait before retry number ``attempt`` (0-based).

    The provider's ``Retry-After`` wins when present — it is the one authoritative
    number. Otherwise: exponential from ``base_seconds``, doubling per attempt, with
    *full* jitter (uniform over the whole window, not just its tail) so N concurrent
    callers that were throttled together do not retry together. Either way clamped to
    ``max_seconds``, which bounds how long one judge call can hold its worker thread.
    """
    hinted = _retry_after_seconds(exc)
    if hinted is not None:
        return min(hinted, policy.max_seconds)
    window = min(policy.base_seconds * (2**attempt), policy.max_seconds)
    return random.uniform(0.0, window)


def _sleep(seconds: float) -> None:
    """Block for ``seconds``.

    An indirection over ``time.sleep`` purely so tests can record the backoff waits
    without really sleeping and without patching ``time.sleep`` globally. Blocking is
    correct here: judge calls already run in an ``asyncio.to_thread`` worker (see
    ``core.runner._evaluate_metrics``), never on the event loop.
    """
    time.sleep(seconds)


def judge_call(operation: Callable[[], T], *, provider: str, model: str, action: str) -> T:
    """Run ``operation``, retrying provider throttling and naming any other failure.

    Takes a callable rather than being a context manager because a retry has to re-run
    the call, and a ``with`` block's body cannot be re-entered. ``operation`` must be a
    plain LLM request (no side effects), which is what makes replaying it safe.

    A capacity failure (see :data:`RETRYABLE_STATUSES`) is slept off and retried up to
    ``retry_policy().max_attempts``; anything else — and a capacity failure that outlives
    the budget — is wrapped in ``JudgeError`` naming the provider, model, and step, since
    a raw SDK/transport exception otherwise propagates with no hint of *which* judge call
    failed. ``JudgeError`` is re-raised untouched (already descriptive), and the original
    exception is chained via ``from`` so the traceback is preserved.
    """
    policy = retry_policy()
    for attempt in range(policy.max_attempts):
        try:
            return operation()
        except JudgeError:
            raise
        except Exception as exc:
            remaining = policy.max_attempts - attempt - 1
            if remaining and _is_capacity_error(exc):
                delay = _backoff_seconds(attempt, policy, exc)
                logger.warning(
                    "Juez de %s (modelo %r) limitado durante %s: %s. "
                    "Reintento %d/%d en %.2fs",
                    provider,
                    model,
                    action,
                    exc,
                    attempt + 1,
                    policy.max_attempts - 1,
                    delay,
                )
                _sleep(delay)
                continue
            raise JudgeError(
                f"Falló la llamada al juez de {provider} (modelo {model!r}) durante "
                f"{action}: {type(exc).__name__}: {exc}"
            ) from exc
    # Unreachable: the loop either returns or raises on its last attempt (max_attempts is
    # clamped to >= 1). Present so every path has a return for the type checker.
    raise AssertionError("judge_call agotó el bucle de reintentos sin resultado")


def require_parsed(
    parsed: T | None,
    *,
    provider: str,
    model: str,
    action: str,
    refusal: str | None = None,
) -> T:
    """Return ``parsed`` or raise a descriptive ``JudgeError`` when it is ``None``.

    Structured-output calls return ``None`` when the model refuses or emits output
    that does not satisfy the requested schema; reading ``.score`` off that ``None``
    would raise an opaque ``AttributeError``. This turns it into an actionable
    Spanish message that includes the model, the step, and any refusal text.
    """
    if parsed is not None:
        return parsed
    detail = f" El modelo rechazó la petición: {refusal}." if refusal else ""
    raise JudgeError(
        f"El juez de {provider} (modelo {model!r}) no devolvió una respuesta "
        f"estructurada válida durante {action}. La salida del modelo no cumplió el "
        f"esquema solicitado o la petición fue rechazada.{detail}"
    )


def owned_model(model: str, *, owns: Callable[[str], bool], provider: str) -> str:
    """Return ``model`` if it belongs to ``provider``, else raise a Spanish error.

    ``owns`` is the judge's provider-ownership predicate. Used to validate both a
    step-resolved model and an explicitly requested one before it reaches the SDK,
    so a judge never issues a call for a model of a different provider.
    """
    if not owns(model):
        raise ValueError(
            f"El modelo {model!r} no pertenece al proveedor {provider}. "
            f"Usa un modelo de {provider}."
        )
    return model


class _ScoreResponse(BaseModel):
    """The structured result a judge requests from the model for a rubric score."""

    score: float
    justification: str  # Spanish rationale


class ScaleSpec(NamedTuple):
    """A scale's numeric bounds plus a Spanish instruction describing its range."""

    lo: float
    hi: float
    is_boolean: bool
    instruction_es: str


def scale_spec(scale: Scale) -> ScaleSpec:
    """Derive numeric bounds and a Spanish range instruction from a ``Scale``.

    ``Boolean`` is checked before the others because it is a distinct pass/fail
    scale; unknown scales fall back to a safe ``[0, 1]`` range.
    """
    if isinstance(scale, Boolean):
        return ScaleSpec(0.0, 1.0, True, "Asigna 1 si cumple o 0 si no cumple.")
    if isinstance(scale, Likert):
        return ScaleSpec(
            scale.lo,
            scale.hi,
            False,
            f"Asigna una puntuación entre {scale.lo:g} y {scale.hi:g}.",
        )
    if isinstance(scale, Unit):
        return ScaleSpec(0.0, 1.0, False, "Asigna un número entre 0.0 y 1.0.")
    return ScaleSpec(0.0, 1.0, False, "Asigna un número entre 0.0 y 1.0.")


class _SafeDict(dict):
    """Format map that leaves unknown ``{placeholders}`` untouched."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fill_placeholders(template: str, turn: TurnView) -> str:
    """Substitute ``{prompt}``/``{response}``/``{context}`` in ``template``.

    ``{context}`` expands to the retrieved-context blob (empty when there is none).
    Rubric text may contain stray braces (e.g. JSON examples); fall back to the raw
    template if it cannot be formatted rather than raising.
    """
    try:
        return template.format_map(
            _SafeDict(
                prompt=turn.prompt,
                response=turn.response,
                context=turn.retrieved_context.render(),
            )
        )
    except (ValueError, IndexError, KeyError):
        return template


def render_prompt(instructions: str, turn: TurnView) -> str:
    """Render a rubric/instruction plus a structured Spanish view of the turn.

    ``instructions`` is filled with the turn's ``{prompt}``/``{response}``/
    ``{context}`` (when it references them), then the full turn — number, history,
    retrieved context, prompt, response — is appended so the model always has
    complete context even if the rubric does not interpolate every field.
    """
    parts = [_fill_placeholders(instructions, turn), "", "--- Turno a evaluar ---"]
    parts.append(f"Número de turno: {turn.turn_number}")
    if turn.history:
        parts.append("Historial de la conversación:")
        for i, (prompt, response) in enumerate(turn.history, start=1):
            parts.append(f"  [{i}] Usuario: {prompt}")
            parts.append(f"      Asistente: {response}")
    if not turn.retrieved_context.is_empty:
        parts.append("--- Contexto recuperado ---")
        parts.append(turn.retrieved_context.render())
    parts.append(f"Usuario: {turn.prompt}")
    parts.append(f"Asistente: {turn.response}")
    return "\n".join(parts)


def clamp(score: float, spec: ScaleSpec) -> float:
    """Clamp a model-produced score into the scale's range (rounding booleans)."""
    bounded = min(max(score, spec.lo), spec.hi)
    if spec.is_boolean:
        return 1.0 if bounded >= 0.5 else 0.0
    return bounded
