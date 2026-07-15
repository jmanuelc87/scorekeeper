"""SDK-free helpers shared by the concrete judges.

Everything an LLM judge needs *around* the model call — the Spanish system
prompt, turning a ``Scale`` into a Spanish range instruction, rendering a rubric
template plus the turn context, and clamping the model's score back into the
scale's range — lives here so it is unit-testable without any LLM SDK and so the
Anthropic and OpenAI judges stay thin wrappers over their respective clients.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.scale import Boolean, Likert, Scale, Unit

# Default system prompt for every judge call. Overridable per judge (constructor
# arg) or globally (``Settings.judge_system_prompt``). All scoring output is Spanish.
DEFAULT_SYSTEM_PROMPT = (
    "Eres un evaluador experto de conversaciones entre usuarios y asistentes de "
    "IA. Evalúas un único turno según una rúbrica y devuelves una puntuación "
    "numérica junto con una justificación breve. Sé objetivo y responde siempre "
    "en español."
)


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
                context=turn.retrieved_context,
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
    if turn.retrieved_context:
        parts.append("--- Contexto recuperado ---")
        parts.append(turn.retrieved_context)
    parts.append(f"Usuario: {turn.prompt}")
    parts.append(f"Asistente: {turn.response}")
    return "\n".join(parts)


def clamp(score: float, spec: ScaleSpec) -> float:
    """Clamp a model-produced score into the scale's range (rounding booleans)."""
    bounded = min(max(score, spec.lo), spec.hi)
    if spec.is_boolean:
        return 1.0 if bounded >= 0.5 else 0.0
    return bounded
