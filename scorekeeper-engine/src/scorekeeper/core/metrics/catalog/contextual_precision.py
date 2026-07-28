"""Contextual precision — does the retriever rank relevant nodes first?

This is a *ranking* metric for the retrieval stage of a RAG turn. Given the
ordered list of retrieved nodes (rank 1 = the node the retriever/re-ranker placed
first), it rewards putting relevant nodes ahead of irrelevant ones. Order is the
entire point: the same set of nodes scores higher when the relevant ones come
first.

It works in two stages:

* **Stage 1 — relevance labeling.** For each node, the judge decides whether the
  node is relevant, judging it against the turn's ``expected_output`` (the ground
  truth), *not* the generator's actual response. Judging against the reference is
  what makes the metric reference-based and keeps ranking evaluation honest even
  when the generator answered badly. The verdict is a binary ``r_k`` in original
  rank order.
* **Stage 2 — weighted cumulative precision.** This is Average Precision: at every
  rank ``k`` where a relevant node appears, add the precision-at-``k`` (relevant
  nodes seen so far / ``k``), then divide by the total number of relevant nodes.
  A relevant node at a high rank contributes a larger precision term, so front-
  loading relevant nodes maximizes the score.

Nodes come from the turn's structured ``retrieved_context`` documents, in retriever
rank order, each rendered to node text (source ``document`` + ``content``) by
``RetrievedContext.node_texts`` (the same node convention the hallucination metric
uses). Labeling is a *classification* step, so it goes through the judge's
``structured()`` seam rather than ``score()``. With no relevant node the score is
``0.0`` — a real measurement of a failed retrieval; with no retrieved context at
all there is no ranking to measure, so the metric returns ``NOT_APPLICABLE`` and
rollup leaves it out of the averages. All prompts and justification output are
Spanish.
"""

from __future__ import annotations

from pydantic import BaseModel

from scorekeeper.core.metrics.base import (
    NOT_APPLICABLE,
    MetricResult,
    MetricTrace,
    MultiStepMetric,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import Judge, JudgeStep
from scorekeeper.core.metrics.prompts import PromptSlot, safe_format
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Unit


class RelevanceVerdict(BaseModel):
    """The judge's binary relevance verdict for one retrieved node."""

    relevant: bool
    justification: str  # Spanish rationale


@register
class ContextualPrecision(MultiStepMetric):
    """Average Precision of relevant nodes over the retriever's ranking."""

    name = "contextual_precision"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1 (weighted cumulative precision)
    weight = 1.0
    prompts = (
        PromptSlot(
            slug="verdict",
            required_variables=("expected_output", "node"),
            description=(
                "Veredicto binario de relevancia de un nodo recuperado frente a la "
                "respuesta esperada, no frente a la respuesta del asistente."
            ),
        ),
    )
    # strict_mode collapses the score to a pass/fail: only a perfect ranking
    # (every relevant node ahead of every irrelevant one → 1.0) passes.
    strict_mode: bool = False

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        nodes = turn.retrieved_context.node_texts()

        if not nodes:
            # No retrieved nodes to rank: there is no ranking to measure.
            summary = (
                "No hay nodos de contexto recuperado que ordenar; la métrica no "
                "aplica a este turno y queda fuera de los promedios."
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=NOT_APPLICABLE,
                normalized_score=NOT_APPLICABLE,
                trace=MetricTrace(
                    steps=[TraceStep(label="Sin nodos recuperados", summary=summary)]
                ),
                rubric_version=self.rubric_version,
            )

        # Stage 1: label each node's relevance against the expected output, in the
        # retriever's original rank order. The node under evaluation is isolated in
        # its own view (empty response, no context blob) so the judge is not biased
        # by the assistant's actual answer or by the other nodes. Each node is a
        # typed entry: value=relevant, metadata carries its rank.
        verdicts: list[int] = []
        label_step = TraceStep(label="Relevancia por nodo")
        node_view = TurnView(prompt=turn.prompt, response="")
        judge_model = judge.model_for(JudgeStep.EXTRACT)
        template = self.prompt("verdict")
        for k, node in enumerate(nodes, start=1):
            verdict = judge.structured(
                instruction=safe_format(
                    template, expected_output=turn.expected_output, node=node
                ),
                turn=node_view,
                schema=RelevanceVerdict,
                step=JudgeStep.EXTRACT,
                model=judge_model,
            )
            r_k = 1 if verdict.relevant else 0
            verdicts.append(r_k)
            label_step.entries.append(
                TraceEntry(
                    label=f"Nodo {k}",
                    value=verdict.relevant,
                    justification=verdict.justification,
                    metadata={"rank": k},
                )
            )

        total_relevant = sum(verdicts)

        if total_relevant == 0:
            result_step = TraceStep(
                label="Precisión contextual",
                summary="Ningún nodo recuperado es relevante; precisión = 0.000.",
                entries=[TraceEntry(label="precisión contextual", value=0.0)],
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                trace=MetricTrace(steps=[label_step, result_step]),
                judge_model=judge_model,
                rubric_version=self.rubric_version,
            )

        # Stage 2: weighted cumulative precision (Average Precision). Each relevant
        # node at rank k contributes precision-at-k = (relevant seen so far / k);
        # dividing by the total relevant count normalizes into [0, 1].
        running_relevant = 0
        weighted_sum = 0.0
        for k, r_k in enumerate(verdicts, start=1):
            running_relevant += r_k
            if r_k == 1:
                weighted_sum += running_relevant / k

        raw = weighted_sum / total_relevant

        if self.strict_mode:
            # Only a perfect ranking passes; anything less collapses to 0.0.
            raw = 1.0 if raw == 1.0 else 0.0

        result_step = TraceStep(
            label="Precisión contextual",
            summary=(
                f"{total_relevant} de {len(nodes)} nodos son relevantes; "
                f"precisión contextual (Average Precision) = {raw:.3f}."
            ),
            entries=[TraceEntry(label="precisión contextual", value=round(raw, 3))],
        )
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=[label_step, result_step]),
            judge_model=judge_model,
            rubric_version=self.rubric_version,
        )
