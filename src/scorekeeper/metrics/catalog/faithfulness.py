"""Faithfulness — two groundedness metrics for RAG turns.

Both measure whether the assistant's answer is grounded in the retrieved
context, but by different published algorithms:

* :class:`FaithfulnessRagas` (RAGAS) — extract statements *from the answer*, then
  verify each *against the context* by positive entailment. Score is the fraction
  of statements that can be inferred from the context.
* :class:`FaithfulnessDeepeval` (DeepEval) — extract *truths from the context* and
  *claims from the answer*, then per claim ask whether the truths *contradict* it.
  Score is the fraction **not** contradicted — an unverifiable claim (not mentioned
  in the truths) passes; only a direct contradiction fails.

Neither metric touches ``retrieved_context`` directly. It is a single Spanish
text blob on ``TurnView``; the judge layer renders it into every prompt (via the
``{context}`` placeholder and an appended "Contexto recuperado" section), so
these metrics stay agnostic to context shape and just hand the turn to the judge.
All prompts and justification output are Spanish.
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


class Claims(BaseModel):
    """Statements/claims extracted from the assistant's answer."""

    claims: list[str] = []
    summary: str = ""


class Truths(BaseModel):
    """Ground-truth facts extracted from the retrieved context."""

    truths: list[str] = []
    summary: str = ""


# --- Spanish prompts ----------------------------------------------------------

# Extraction instructions may reference {prompt}/{response}/{context}; the judge
# fills them.
EXTRACT_CLAIMS = (
    "Crea una o más afirmaciones a partir de cada oración de la respuesta del "
    "asistente. Cada afirmación debe ser un enunciado verificable e independiente. "
    "Devuelve también un breve resumen en español.\n"
    "Pregunta: {prompt}\nRespuesta: {response}"
)

GENERATE_TRUTHS = (
    "Extrae las verdades o hechos presentes en el contexto recuperado. Cada "
    "verdad debe ser un enunciado atómico y verificable tomado únicamente del "
    "contexto. Devuelve también un breve resumen en español.\n"
    "Contexto: {context}"
)

# Verification rubrics: {claim}/{truths} are pre-filled in the metric with
# .format(); they must NOT contain {prompt}/{response}/{context} — the judge
# appends the full turn (including retrieved context) automatically.
VERIFY_RAGAS = (
    "¿Puede inferirse la siguiente afirmación a partir del contexto recuperado? "
    "Asigna 1 si la afirmación se deduce del contexto, o 0 si no se deduce o lo "
    "contradice. Justifica brevemente en español.\n"
    "Afirmación: {claim}"
)

VERIFY_DEEPEVAL = (
    "¿Las siguientes verdades contradicen la afirmación? Asigna 0 SOLO si las "
    "verdades contradicen directamente la afirmación. Asigna 1 si la afirmación "
    "concuerda con las verdades o si no se menciona (no verificable). Justifica "
    "brevemente en español.\n"
    "Verdades:\n{truths}\n"
    "Afirmación: {claim}"
)


# --- Metrics ------------------------------------------------------------------


@register
class FaithfulnessRagas(MultiStepMetric):
    """RAGAS faithfulness: fraction of answer statements entailed by the context."""

    name = "faithfulness_ragas"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        extraction = judge.structured(
            instruction=EXTRACT_CLAIMS, turn=turn, schema=Claims
        )
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=extraction.summary,
            )
        )

        # Nothing to verify → nothing can be unfaithful.
        if not extraction.claims:
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
                rubric=VERIFY_RAGAS.format(claim=claim),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
            )
            for claim in extraction.claims
        ]
        for claim, verdict in zip(extraction.claims, verdicts, strict=True):
            trace.append(
                StepTrace(label=f"Verificación: {claim}", detail=verdict.justification)
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
class FaithfulnessDeepeval(MultiStepMetric):
    """DeepEval faithfulness: fraction of answer claims not contradicted by context."""

    name = "faithfulness_deepeval"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Extract claims first so the "no claims" case short-circuits before we
        # pay for truths extraction. The pseudocode runs the two extractions
        # concurrently (order-independent), so leading with claims is equivalent.
        extraction = judge.structured(
            instruction=EXTRACT_CLAIMS, turn=turn, schema=Claims
        )
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=extraction.summary,
            )
        )
        if not extraction.claims:
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
            instruction=GENERATE_TRUTHS, turn=turn, schema=Truths
        )
        trace.append(
            StepTrace(
                label="Extracción de verdades del contexto", detail=truths.summary
            )
        )

        truths_text = "\n".join(truths.truths)
        verdicts = [
            judge.score(
                rubric=VERIFY_DEEPEVAL.format(truths=truths_text, claim=claim),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
            )
            for claim in extraction.claims
        ]
        for claim, verdict in zip(extraction.claims, verdicts, strict=True):
            trace.append(
                StepTrace(label=f"Veredicto: {claim}", detail=verdict.justification)
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
