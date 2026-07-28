"""Answer relevance.

Measures how directly the answer addresses the original question, following the
*reverse-question generation* method (RAGAS): the LLM is used to generate ``n``
questions the answer could be answering, the original and generated questions are
embedded, and the cosine similarity between the original and each generated
question is averaged. Higher means the answer sticks closer to what was asked.

It is a ``MultiStepMetric`` because it orchestrates several judge calls (``n``
generation steps + one embedding step) and records each as a structured
``MetricTrace`` (generation, per-question similarity, mean). It depends only on
the ``Judge`` seam (``structured`` to generate and ``embed`` to vectorize), so it
imports no SDK.
"""

from __future__ import annotations

import numpy as np
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
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Unit

class GeneratedQuestion(BaseModel):
    """A reverse-generated question produced from the answer."""

    question: str


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors; 0.0 if either is empty or zero."""
    if not a or not b:
        return 0.0
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    norm_a = np.linalg.norm(va)
    norm_b = np.linalg.norm(vb)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


@register
class AnswerRelevance(MultiStepMetric):
    name = "answer_relevance"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1 (mean cosine, clamped to [0, 1])
    weight = 1.0
    prompts = (
        PromptSlot(
            slug="generate_question",
            description=(
                "Generación inversa: a partir de la respuesta sola, produce la "
                "pregunta que estaría respondiendo. La pregunta original no se "
                "expone, para no sesgar la generación."
            ),
        ),
    )
    # Number of questions to reverse-generate (step 1 of the algorithm). A plain
    # class attribute so it can be overridden per instance.
    n_questions: int = 3

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        steps: list[TraceStep] = []

        # Step 1: generate n candidate questions from the ANSWER only. The answer
        # is isolated in its own TurnView so the judge never sees the original
        # question and cannot copy it when generating.
        answer_view = TurnView(prompt="", response=turn.response)
        judge_model = judge.model_for(JudgeStep.EXTRACT)
        # Passed raw: the judge substitutes {response} from ``answer_view``.
        instruction = self.prompt("generate_question")
        questions: list[str] = []
        for _ in range(self.n_questions):
            generated = judge.structured(
                instruction=instruction,
                turn=answer_view,
                schema=GeneratedQuestion,
                step=JudgeStep.EXTRACT,
                model=judge_model,
            )
            question = generated.question.strip()
            if question:
                questions.append(question)

        steps.append(
            TraceStep(
                label="Preguntas generadas a partir de la respuesta",
                summary=f"{len(questions)} pregunta(s) generada(s)",
                entries=[TraceEntry(label=q) for q in questions],
            )
        )

        # With no usable questions there is nothing to compare: the metric does not
        # apply to this turn.
        if not questions:
            steps.append(
                TraceStep(
                    label="Relevancia media",
                    summary=(
                        "No se generaron preguntas; la métrica no aplica a este "
                        "turno y queda fuera de los promedios."
                    ),
                )
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=NOT_APPLICABLE,
                normalized_score=NOT_APPLICABLE,
                trace=MetricTrace(steps=steps),
                judge_model=judge_model,
                rubric_version=self.rubric_version,
            )

        # Step 2: embed the original question and all generated ones in one call.
        embeddings = judge.embed(texts=[turn.prompt, *questions])
        e_q, e_generated = embeddings[0], embeddings[1:]

        # Step 3: average the cosine similarity between the original question and
        # each generated question. Each similarity is a typed entry.
        similarities = [cosine_similarity(e_q, e_qi) for e_qi in e_generated]
        steps.append(
            TraceStep(
                label="Similitud por pregunta",
                entries=[
                    TraceEntry(label=q, value=round(sim, 3))
                    for q, sim in zip(questions, similarities, strict=True)
                ],
            )
        )

        mean = sum(similarities) / len(similarities)
        # Cosine lives in [-1, 1]; clamp to [0, 1] for the Unit scale and rollup.
        raw = max(0.0, min(1.0, mean))
        steps.append(
            TraceStep(
                label="Relevancia media",
                summary=f"Media de similitudes = {raw:.3f}",
                entries=[TraceEntry(label="media", value=round(raw, 3))],
            )
        )

        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=steps),
            judge_model=getattr(judge, "model", None),
            rubric_version=self.rubric_version,
        )
