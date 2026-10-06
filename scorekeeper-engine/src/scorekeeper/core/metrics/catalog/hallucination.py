"""Hallucination — how far a RAG answer departs from its retrieved context.

The answer *hallucinates* when it contradicts the retrieved context. We score it
the way a natural-language-inference (NLI) model would, but with the
LLM-as-a-judge standing in for a trained SNLI classifier: for each retrieved
context document (the *premise*) we ask the judge to classify the model's answer
(the *hypothesis*) as **entailment / neutral / contradiction**, then count how
many documents the answer contradicts.

    hallucination = contradicted / len(context_docs)   # 0 faithful … 1 fully hallucinated

We store the hallucination rate as the raw score (higher = worse). Rollup averages
*normalized* scores as higher-is-better, so the metric uses an ``Inverted(Unit())``
scale: the raw score keeps its intuitive direction while normalization maps it to
the faithfulness complement (``1 - rate``), which composes correctly with the other
metrics. When a turn has no retrieved context there is nothing to contradict and
nothing to measure, so the metric returns ``NOT_APPLICABLE`` with no judge calls
and rollup leaves it out of the averages.

The NLI judgment is a multiple-choice decision over the three labels, so it goes
through the judge's ``choose()`` seam rather than ``score()``.
"""

from __future__ import annotations

from enum import StrEnum

from scorekeeper.core.metrics.base import (
    NOT_APPLICABLE,
    MetricResult,
    MetricTrace,
    MultiStepMetric,
    TraceEntry,
    TraceStep,
    TurnView,
    context_documents,
    decision_metadata,
)
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import Judge, JudgeStep
from scorekeeper.core.metrics.prompts import PromptSlot, safe_format
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Inverted, Unit


class NLILabel(StrEnum):
    """The three natural-language-inference classes (premise → hypothesis)."""

    ENTAILMENT = "entailment"
    NEUTRAL = "neutral"
    CONTRADICTION = "contradiction"


def split_context_docs(context: str) -> list[str]:
    """Split a ``retrieved_context`` value into individual retrieved documents.

    Thin alias over :func:`context_documents`, which handles both the extension's
    JSON citation array and the spreadsheet text blob. Kept as a named export for
    the metrics (and tests) that document their dependency on document splitting.
    """
    return context_documents(context)


@register
class Hallucination(MultiStepMetric):
    """Fraction of retrieved documents the answer contradicts."""

    name = "hallucination"
    category = MetricCategory.SEGURIDAD
    # Raw score is the hallucination rate (0-1, higher = worse). Inverted() flips
    # it to a higher-is-better faithfulness value at normalization so it rolls up
    # correctly alongside the other metrics.
    scale = Inverted(Unit())
    weight = 1.0
    prompts = (
        PromptSlot(
            slug="nli",
            required_variables=("documento", "response"),
            description=(
                "Clasificación NLI de la respuesta (hipótesis) frente a un documento "
                "recuperado (premisa): entailment, contradiction o neutral."
            ),
        ),
    )

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        docs = turn.retrieved_context.node_texts()

        if not docs:
            # No retrieved context to contradict: the metric does not apply here.
            summary = (
                "No hay contexto recuperado para verificar; la métrica no aplica "
                "a este turno y queda fuera de los promedios."
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=NOT_APPLICABLE,
                normalized_score=NOT_APPLICABLE,
                trace=MetricTrace(
                    steps=[TraceStep(label="Sin contexto recuperado", summary=summary)]
                ),
                rubric_version=self.rubric_version,
            )

        # One NLI classification per document; the label is a typed entry value.
        contradicted = 0
        template = self.prompt("nli")
        # The template defines each label, so the options carry no descriptions.
        options = {label.value: None for label in NLILabel}
        nli_step = TraceStep(label="Clasificación NLI por documento")
        for i, doc in enumerate(docs, start=1):
            judgment = judge.choose(
                instruction=safe_format(template, documento=doc, response=turn.response),
                turn=turn,
                options=options,
                step=JudgeStep.EXTRACT,
            )
            # The model that answered (a decision judge may not be the step-routed one).
            judge_model = judgment.model
            if judgment.choice == NLILabel.CONTRADICTION:
                contradicted += 1
            nli_step.entries.append(
                TraceEntry(
                    label=f"Documento {i}",
                    value=judgment.choice,
                    justification=judgment.justification,
                    metadata=decision_metadata(judgment),
                )
            )

        raw = contradicted / len(docs)
        result_step = TraceStep(
            label="Resultado",
            summary=(
                f"{contradicted} de {len(docs)} documentos contradicen la "
                f"respuesta (tasa de alucinación {raw:.2f})."
            ),
            entries=[TraceEntry(label="tasa de alucinación", value=round(raw, 4))],
        )
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=[nli_step, result_step]),
            judge_model=judge_model,
            rubric_version=self.rubric_version,
        )
