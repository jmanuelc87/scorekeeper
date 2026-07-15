"""Answer relevance.

Measures how directly the answer addresses the original question, following the
*reverse-question generation* method (RAGAS): the LLM is used to generate ``n``
questions the answer could be answering, the original and generated questions are
embedded, and the cosine similarity between the original and each generated
question is averaged. Higher means the answer sticks closer to what was asked.

It is a ``MultiStepMetric`` because it orchestrates several judge calls (``n``
generation steps + one embedding step) and flattens the trace into a single
Spanish justification. It depends only on the ``Judge`` seam (``structured`` to
generate and ``embed`` to vectorize), so it imports no SDK.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from scorekeeper.metrics.base import MetricResult, MultiStepMetric, StepTrace, TurnView
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Unit

# Reverse-generation instruction: from the answer ALONE, produce a question the
# answer would be answering. The original question is not exposed so generation
# is not biased toward it. (Prompt text stays Spanish — it is sent to the LLM.)
GENERATE_QUESTION = """\
Genera una única pregunta en español que la siguiente respuesta estaría \
respondiendo. Devuelve solo la pregunta, sin explicaciones ni comentarios.
respuesta: {response}
"""


class GeneratedQuestion(BaseModel):
    """A reverse-generated question produced from the answer."""

    question: str


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors; 0.0 if either is empty or zero."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@register
class AnswerRelevance(MultiStepMetric):
    name = "answer_relevance"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1 (mean cosine, clamped to [0, 1])
    weight = 1.0
    # Number of questions to reverse-generate (step 1 of the algorithm). A plain
    # class attribute so it can be overridden per instance.
    n_questions: int = 3

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Step 1: generate n candidate questions from the ANSWER only. The answer
        # is isolated in its own TurnView so the judge never sees the original
        # question and cannot copy it when generating.
        answer_view = TurnView(prompt="", response=turn.response)
        questions: list[str] = []
        for _ in range(self.n_questions):
            generated = judge.structured(
                instruction=GENERATE_QUESTION,
                turn=answer_view,
                schema=GeneratedQuestion,
            )
            question = generated.question.strip()
            if question:
                questions.append(question)

        questions_detail = "\n".join(f"- {q}" for q in questions) or "(ninguna)"
        trace.append(
            StepTrace(label="Preguntas generadas a partir de la respuesta", detail=questions_detail)
        )

        # With no usable questions there is nothing to compare: relevance is zero.
        if not questions:
            trace.append(
                StepTrace(
                    label="Relevancia media",
                    detail="No se generaron preguntas; relevancia = 0.000.",
                )
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                justification=self.render_justification(trace),
                judge_model=getattr(judge, "model", None),
                rubric_version=self.rubric_version,
                trace=trace,
            )

        # Step 2: embed the original question and all generated ones in one call.
        embeddings = judge.embed(texts=[turn.prompt, *questions])
        e_q, e_generated = embeddings[0], embeddings[1:]

        # Step 3: average the cosine similarity between the original question and
        # each generated question.
        similarities = [cosine_similarity(e_q, e_qi) for e_qi in e_generated]
        for q, sim in zip(questions, similarities, strict=True):
            trace.append(StepTrace(label=f"Similitud: {q}", detail=f"coseno = {sim:.3f}"))

        mean = sum(similarities) / len(similarities)
        # Cosine lives in [-1, 1]; clamp to [0, 1] for the Unit scale and rollup.
        raw = max(0.0, min(1.0, mean))
        trace.append(
            StepTrace(label="Relevancia media", detail=f"Media de similitudes = {raw:.3f}")
        )

        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=getattr(judge, "model", None),
            rubric_version=self.rubric_version,
            trace=trace,
        )
