"""Faithfulness (fidelidad) — two groundedness metrics for RAG turns.

Both measure whether the assistant's answer is grounded in the retrieved
context, but by different published algorithms:

* :class:`FidelidadRagas` (RAGAS) — extract statements *from the answer*, then
  verify each *against the context* by positive entailment. Score is the
  fraction of statements that can be inferred from the context.
* :class:`FidelidadDeepeval` (DeepEval) — extract *truths from the context* and
  *claims from the answer*, then per claim ask whether the truths *contradict*
  it. Score is the fraction **not** contradicted — an unverifiable claim (not
  mentioned in the truths) passes; only a direct contradiction fails.

Neither metric touches ``retrieved_context`` directly. It is a single Spanish
text blob on ``TurnView``; the judge layer renders it into every prompt (via the
``{context}`` placeholder and an appended "Contexto recuperado" section), so
these metrics stay agnostic to context shape and just hand the turn to the judge.
All prompts and output are Spanish.
"""

from __future__ import annotations

from pydantic import BaseModel

from scorekeeper.metrics.base import (
    MetricResult,
    MultiStepMetric,
    StepTrace,
    TurnView,
)
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Boolean, Unit


# --- Extraction schemas -------------------------------------------------------


class Afirmaciones(BaseModel):
    """Statements/claims extracted from the assistant's answer."""

    afirmaciones: list[str] = []
    summary: str = ""


class Verdades(BaseModel):
    """Ground-truth facts extracted from the retrieved context."""

    verdades: list[str] = []
    summary: str = ""


# --- Spanish prompts ----------------------------------------------------------

# Extraction instructions may reference {prompt}/{response}; the judge fills them.
EXTRAER_AFIRMACIONES = (
    "Crea una o más afirmaciones a partir de cada oración de la respuesta del "
    "asistente. Cada afirmación debe ser un enunciado verificable e independiente. "
    "Devuelve también un breve resumen en español.\n"
    "Pregunta: {prompt}\nRespuesta: {response}"
)

GENERAR_VERDADES = (
    "Extrae las verdades o hechos presentes en el contexto recuperado. Cada "
    "verdad debe ser un enunciado atómico y verificable tomado únicamente del "
    "contexto. Devuelve también un breve resumen en español.\n"
    "Contexto: {context}"
)

# Verification rubrics: {afirmacion}/{verdades} are pre-filled in the metric with
# .format(); they must NOT contain {prompt}/{response}/{context} — the judge
# appends the full turn (including retrieved context) automatically.
VERIFICAR_RAGAS = (
    "¿Puede inferirse la siguiente afirmación a partir del contexto recuperado? "
    "Asigna 1 si la afirmación se deduce del contexto, o 0 si no se deduce o lo "
    "contradice. Justifica brevemente en español.\n"
    "Afirmación: {afirmacion}"
)

VERIFICAR_DEEPEVAL = (
    "¿Las siguientes verdades contradicen la afirmación? Asigna 0 SOLO si las "
    "verdades contradicen directamente la afirmación. Asigna 1 si la afirmación "
    "concuerda con las verdades o si no se menciona (no verificable). Justifica "
    "brevemente en español.\n"
    "Verdades:\n{verdades}\n"
    "Afirmación: {afirmacion}"
)


# --- Metrics ------------------------------------------------------------------


@register
class FidelidadRagas(MultiStepMetric):
    """RAGAS faithfulness: fraction of answer statements entailed by the context."""

    name = "fidelidad_ragas"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        extraction = judge.structured(
            instruction=EXTRAER_AFIRMACIONES, turn=turn, schema=Afirmaciones
        )
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=extraction.summary,
            )
        )

        # Nothing to verify → nothing can be unfaithful.
        if not extraction.afirmaciones:
            return MetricResult(
                metric_name=self.name,
                raw_score=1.0,
                normalized_score=self.normalize(1.0),
                justification=self.render_justification(trace),
                judge_model=None,
                rubric_version=self.rubric_version,
                trace=trace,
            )

        verdicts = [
            judge.score(
                rubric=VERIFICAR_RAGAS.format(afirmacion=afirmacion),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
            )
            for afirmacion in extraction.afirmaciones
        ]
        for afirmacion, verdict in zip(
            extraction.afirmaciones, verdicts, strict=True
        ):
            trace.append(
                StepTrace(
                    label=f"Verificación: {afirmacion}", detail=verdict.justification
                )
            )

        # Boolean scale → each score is 0/1; mean = supported / n.
        raw = sum(v.score for v in verdicts) / len(verdicts)
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=verdicts[0].model,
            rubric_version=self.rubric_version,
            trace=trace,
        )


@register
class FidelidadDeepeval(MultiStepMetric):
    """DeepEval faithfulness: fraction of answer claims not contradicted by context."""

    name = "fidelidad_deepeval"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Extract claims first so the "no claims" case short-circuits before we
        # pay for truths extraction. The pseudocode runs the two extractions
        # concurrently (order-independent), so leading with claims is equivalent.
        claims = judge.structured(
            instruction=EXTRAER_AFIRMACIONES, turn=turn, schema=Afirmaciones
        )
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=claims.summary,
            )
        )
        if not claims.afirmaciones:
            return MetricResult(
                metric_name=self.name,
                raw_score=1.0,
                normalized_score=self.normalize(1.0),
                justification=self.render_justification(trace),
                judge_model=None,
                rubric_version=self.rubric_version,
                trace=trace,
            )

        truths = judge.structured(
            instruction=GENERAR_VERDADES, turn=turn, schema=Verdades
        )
        trace.append(
            StepTrace(
                label="Extracción de verdades del contexto", detail=truths.summary
            )
        )

        verdades_texto = "\n".join(truths.verdades)
        verdicts = [
            judge.score(
                rubric=VERIFICAR_DEEPEVAL.format(
                    verdades=verdades_texto, afirmacion=afirmacion
                ),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
            )
            for afirmacion in claims.afirmaciones
        ]
        for afirmacion, verdict in zip(claims.afirmaciones, verdicts, strict=True):
            trace.append(
                StepTrace(
                    label=f"Veredicto: {afirmacion}", detail=verdict.justification
                )
            )

        # Boolean scale → contradicted=0, otherwise 1; mean = not_contradicted / n.
        raw = sum(v.score for v in verdicts) / len(verdicts)
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=verdicts[0].model,
            rubric_version=self.rubric_version,
            trace=trace,
        )
