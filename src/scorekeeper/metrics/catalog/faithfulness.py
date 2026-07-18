"""Faithfulness — two groundedness metrics for RAG turns.

Both measure whether the assistant's answer is grounded in the retrieved
context, but by different published algorithms. In both, the answer is
decomposed into claims deterministically with :func:`split_sentences` (the
``syntok`` sentence segmenter) — one sentence of the answer is one claim — rather
than by an LLM extraction call. Only the per-claim verification is left to the
judge:

* :class:`FaithfulnessRagas` (RAGAS) — take the answer's sentences as statements,
  then verify each *against the context* by positive entailment. Score is the
  fraction of statements that can be inferred from the context.
* :class:`FaithfulnessDeepeval` (DeepEval) — extract *truths from the context*
  (still an LLM step) and take the answer's sentences as *claims*, then per claim
  ask whether the truths *contradict* it. Score is the fraction **not**
  contradicted — an unverifiable claim (not mentioned in the truths) passes; only
  a direct contradiction fails.

Neither metric touches ``retrieved_context`` directly. It is a single Spanish
text blob on ``TurnView``; the judge layer renders it into every prompt (via the
``{context}`` placeholder and an appended "Contexto recuperado" section), so
these metrics stay agnostic to context shape and just hand the turn to the judge.
All prompts and justification output are Spanish.
"""

from __future__ import annotations

import syntok.segmenter as segmenter
from pydantic import BaseModel

from scorekeeper.metrics.base import (
    MetricResult,
    MultiStepMetric,
    StepTrace,
    TurnView,
)
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge, JudgeStep
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Boolean, Unit


def split_sentences(text: str) -> list[str]:
    """Segment a text blob into sentences with syntok (deterministic, no LLM).

    Each sentence's surface text is reconstructed from its tokens
    (``token.spacing + token.value``), trimmed, and empty fragments are dropped.
    Empty or whitespace-only input yields an empty list.
    """
    sentences: list[str] = []
    for paragraph in segmenter.analyze(text):
        for sentence in paragraph:
            rebuilt = "".join(token.spacing + token.value for token in sentence).strip()
            if rebuilt:
                sentences.append(rebuilt)
    return sentences


def _describe_claims(claims: list[str]) -> str:
    """Spanish trace detail listing the sentences taken as claims."""
    if not claims:
        return "No se extrajo ninguna afirmación de la respuesta."
    listado = "\n".join(f"- {claim}" for claim in claims)
    return f"{len(claims)} afirmación(es) extraída(s) de la respuesta:\n{listado}"


# --- Extraction schemas -------------------------------------------------------


class Truths(BaseModel):
    """Ground-truth facts extracted from the retrieved context."""

    truths: list[str] = []
    summary: str = ""


# --- Spanish prompts ----------------------------------------------------------

# Extraction instructions may reference {prompt}/{response}/{context}; the judge
# fills them.
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


@register(scenarios=["document_retrieval", "web_search"])
class FaithfulnessRagas(MultiStepMetric):
    """RAGAS faithfulness: fraction of answer statements entailed by the context."""

    name = "faithfulness_ragas"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Decompose the answer into claims deterministically: one sentence = one
        # claim (syntok), replacing the former LLM extraction call.
        claims = split_sentences(turn.response)
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=_describe_claims(claims),
            )
        )

        # Nothing to verify → nothing can be unfaithful.
        if not claims:
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
                step=JudgeStep.VERIFY,
                model=judge.model_for(JudgeStep.VERIFY),
            )
            for claim in claims
        ]
        for claim, verdict in zip(claims, verdicts, strict=True):
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


@register(scenarios=["document_retrieval", "web_search"])
class FaithfulnessDeepeval(MultiStepMetric):
    """DeepEval faithfulness: fraction of answer claims not contradicted by context."""

    name = "faithfulness_deepeval"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Split claims first so the "no claims" case short-circuits before we pay
        # for the (still LLM-based) truths extraction. One sentence = one claim.
        claims = split_sentences(turn.response)
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=_describe_claims(claims),
            )
        )
        if not claims:
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
            instruction=GENERATE_TRUTHS,
            turn=turn,
            schema=Truths,
            step=JudgeStep.EXTRACT,
            model=judge.model_for(JudgeStep.EXTRACT),
        )
        trace.append(
            StepTrace(
                label="Extracción de verdades del contexto", detail=truths.summary
            )
        )

        # No truths extracted → nothing to verify claims against. We cannot attest
        # groundedness, so fail closed (0.0) rather than pass every claim as
        # "no verificable", which would silently score fabricated answers as perfect.
        if not truths.truths:
            trace.append(
                StepTrace(
                    label="Verdades vacías",
                    detail=(
                        "No se extrajeron verdades del contexto; sin base para "
                        "verificar las afirmaciones. Se asigna 0."
                    ),
                )
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                justification=self.render_justification(trace),
                judge_model=None,
                rubric_version=self.rubric_version,
                trace=trace,
            )

        truths_text = "\n".join(truths.truths)
        verdicts = [
            judge.score(
                rubric=VERIFY_DEEPEVAL.format(truths=truths_text, claim=claim),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
                step=JudgeStep.VERIFY,
                model=judge.model_for(JudgeStep.VERIFY),
            )
            for claim in claims
        ]
        for claim, verdict in zip(claims, verdicts, strict=True):
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
