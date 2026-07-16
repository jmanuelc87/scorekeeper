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

Nodes come from the turn's ``retrieved_context`` blob, split into ordered,
blank-line-separated blocks (the same node convention the hallucination metric
uses). Labeling is a *classification* step, so it goes through the judge's
``structured()`` seam rather than ``score()``. With no relevant node (or no
retrieved context at all) the score is ``0.0``. All prompts and justification
output are Spanish.
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
from scorekeeper.metrics.catalog.hallucination import split_context_docs
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Unit


class RelevanceVerdict(BaseModel):
    """The judge's binary relevance verdict for one retrieved node."""

    relevant: bool
    justification: str  # Spanish rationale


# Relevance-labeling instruction. Only ``{expected_output}`` and ``{node}`` are
# interpolated (with .format() in the metric); it deliberately contains no other
# ``{...}`` so formatting never trips on stray braces. The turn's input question
# is appended automatically by the judge, so the rubric does not repeat it. The
# node is judged against the EXPECTED answer, never the assistant's response.
VERDICT_PROMPT = """\
Eres un evaluador de recuperación (retrieval) para un sistema RAG. Debes decidir \
si un NODO recuperado es relevante para poder construir la RESPUESTA ESPERADA a \
la pregunta del usuario.

Un nodo es RELEVANTE si aporta información que ayuda directamente a llegar a la \
respuesta esperada (un hecho, dato, definición o paso que aparece o se usa en \
ella). Es NO RELEVANTE si trata de otro tema, es genérico o no contribuye a la \
respuesta esperada, aunque esté relacionado por encima.

Juzga únicamente la utilidad del nodo respecto a la respuesta esperada; no \
evalúes la respuesta del asistente ni la redacción del nodo.

RESPUESTA ESPERADA (verdad de referencia):
{expected_output}

NODO RECUPERADO:
{node}

Devuelve tu veredicto: relevant=true si el nodo es relevante, relevant=false si \
no lo es, junto con una justificación breve en español.
"""


@register
class ContextualPrecision(MultiStepMetric):
    """Average Precision of relevant nodes over the retriever's ranking."""

    name = "contextual_precision"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1 (weighted cumulative precision)
    weight = 1.0
    # strict_mode collapses the score to a pass/fail: only a perfect ranking
    # (every relevant node ahead of every irrelevant one → 1.0) passes.
    strict_mode: bool = False

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        nodes = split_context_docs(turn.retrieved_context)

        if not nodes:
            # No retrieved nodes to rank: nothing relevant was retrieved.
            justification = (
                "No hay nodos de contexto recuperado que ordenar; la precisión "
                "contextual es 0.000."
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                justification=justification,
                rubric_version=self.rubric_version,
            )

        # Stage 1: label each node's relevance against the expected output, in the
        # retriever's original rank order. The node under evaluation is isolated in
        # its own view (empty response, no context blob) so the judge is not biased
        # by the assistant's actual answer or by the other nodes.
        trace: list[StepTrace] = []
        verdicts: list[int] = []
        node_view = TurnView(prompt=turn.prompt, response="")
        judge_model = getattr(judge, "model", None)
        for k, node in enumerate(nodes, start=1):
            verdict = judge.structured(
                instruction=VERDICT_PROMPT.format(
                    expected_output=turn.expected_output, node=node
                ),
                turn=node_view,
                schema=RelevanceVerdict,
            )
            r_k = 1 if verdict.relevant else 0
            verdicts.append(r_k)
            etiqueta = "relevante" if r_k else "no relevante"
            trace.append(
                StepTrace(
                    label=f"Nodo {k} (rango {k}): {etiqueta}",
                    detail=verdict.justification,
                )
            )

        total_relevant = sum(verdicts)

        if total_relevant == 0:
            trace.append(
                StepTrace(
                    label="Precisión contextual",
                    detail="Ningún nodo recuperado es relevante; precisión = 0.000.",
                )
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                justification=self.render_justification(trace),
                judge_model=judge_model,
                rubric_version=self.rubric_version,
                trace=trace,
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

        trace.append(
            StepTrace(
                label="Precisión contextual",
                detail=(
                    f"{total_relevant} de {len(nodes)} nodos son relevantes; "
                    f"precisión contextual (Average Precision) = {raw:.3f}."
                ),
            )
        )
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=judge_model,
            rubric_version=self.rubric_version,
            trace=trace,
        )
