"""Relevancia de la respuesta (answer relevance).

Mide cuán directamente la respuesta aborda la pregunta original, siguiendo el
método de *reverse-question generation* (RAGAS): se usa el LLM para generar ``n``
preguntas que la respuesta podría estar contestando, se embeben la pregunta
original y las generadas, y se promedia la similitud del coseno entre la original
y cada una. Cuanto mayor, más se ciñe la respuesta a lo que se preguntó.

Es un ``MultiStepMetric`` porque orquesta varias llamadas al juez (``n`` pasos de
generación + un paso de *embedding*) y aplana el rastro en una única
justificación en español. Depende solo del seam ``Judge`` (``structured`` para
generar y ``embed`` para vectorizar), por lo que no importa ningún SDK.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from scorekeeper.metrics.base import MetricResult, MultiStepMetric, StepTrace, TurnView
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Unit

# Instrucción de generación inversa: a partir SOLO de la respuesta, produce una
# pregunta que dicha respuesta estaría contestando. No se expone la pregunta
# original para no sesgar la generación hacia ella.
GENERAR_PREGUNTA = """\
Genera una única pregunta en español que la siguiente respuesta estaría \
respondiendo. Devuelve solo la pregunta, sin explicaciones ni comentarios.
respuesta: {response}
"""


class PreguntaGenerada(BaseModel):
    """Una pregunta inversa generada a partir de la respuesta."""

    pregunta: str


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Similitud del coseno entre dos vectores; 0.0 si alguno es nulo o vacío."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@register
class RelevanciaRespuesta(MultiStepMetric):
    name = "relevancia_respuesta"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1 (coseno medio, acotado a [0, 1])
    weight = 1.0
    # Número de preguntas a generar de forma inversa (paso 1 del algoritmo).
    # Atributo de clase simple para permitir sobrescribirlo por instancia.
    n_questions: int = 3

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Paso 1: generar n preguntas candidatas SOLO a partir de la respuesta.
        # Se aísla la respuesta en un TurnView propio para que el juez no vea la
        # pregunta original y no la copie al generar.
        answer_view = TurnView(prompt="", response=turn.response)
        questions: list[str] = []
        for _ in range(self.n_questions):
            generated = judge.structured(
                instruction=GENERAR_PREGUNTA,
                turn=answer_view,
                schema=PreguntaGenerada,
            )
            pregunta = generated.pregunta.strip()
            if pregunta:
                questions.append(pregunta)

        detalle_preguntas = "\n".join(f"- {q}" for q in questions) or "(ninguna)"
        trace.append(
            StepTrace(label="Preguntas generadas a partir de la respuesta", detail=detalle_preguntas)
        )

        # Sin preguntas utilizables no hay nada que comparar: relevancia nula.
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

        # Paso 2: embeber la pregunta original y todas las generadas en una llamada.
        embeddings = judge.embed(texts=[turn.prompt, *questions])
        e_q, e_generadas = embeddings[0], embeddings[1:]

        # Paso 3: promediar la similitud del coseno entre la pregunta original y
        # cada pregunta generada.
        similitudes = [cosine_similarity(e_q, e_qi) for e_qi in e_generadas]
        for q, sim in zip(questions, similitudes, strict=True):
            trace.append(StepTrace(label=f"Similitud: {q}", detail=f"coseno = {sim:.3f}"))

        media = sum(similitudes) / len(similitudes)
        # El coseno vive en [-1, 1]; se acota a [0, 1] para la escala Unit y el rollup.
        raw = max(0.0, min(1.0, media))
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
