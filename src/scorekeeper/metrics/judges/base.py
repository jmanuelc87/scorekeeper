"""SDK-free helpers shared by the concrete judges.

Everything an LLM judge needs *around* the model call — the Spanish system
prompt, turning a ``Scale`` into a Spanish range instruction, rendering a rubric
template plus the turn context, and clamping the model's score back into the
scale's range — lives here so it is unit-testable without any LLM SDK and so the
Anthropic and OpenAI judges stay thin wrappers over their respective clients.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from typing import NamedTuple, TypeVar

from pydantic import BaseModel

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.judge import JudgeStep
from scorekeeper.metrics.scale import Boolean, Likert, Scale, Unit

# Default system prompt for every judge call. Overridable per judge (constructor
# arg) or globally (``Settings.judge_system_prompt``). All scoring output is Spanish.
DEFAULT_SYSTEM_PROMPT = (
    "Eres un evaluador experto de conversaciones entre usuarios y asistentes de "
    "IA. Evalúas un único turno según una rúbrica y devuelves una puntuación "
    "numérica junto con una justificación breve. Sé objetivo y responde siempre "
    "en español."
)


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


@contextmanager
def judge_call(*, provider: str, model: str, action: str) -> Generator[None]:
    """Wrap an SDK call so any failure names the provider, model, and step.

    A raw SDK/transport exception (auth, rate limit, network, 400 for a bad
    parameter) otherwise propagates with no hint of *which* judge call failed.
    Re-raises ``JudgeError`` untouched (already descriptive) and wraps everything
    else, chaining the original via ``from`` so the traceback is preserved.
    """
    try:
        yield
    except JudgeError:
        raise
    except Exception as exc:
        raise JudgeError(
            f"Falló la llamada al juez de {provider} (modelo {model!r}) durante "
            f"{action}: {type(exc).__name__}: {exc}"
        ) from exc


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
