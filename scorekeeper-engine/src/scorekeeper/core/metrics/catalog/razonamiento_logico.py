"""Razonamiento lógico.

Calidad de la lógica, coherencia de argumentos y justificación de conclusiones.

Una sola rúbrica española de 0 a 100 y una sola llamada al juez, así que la clase
es pura declaración: el texto de la rúbrica vive en ``prompt_versions`` y llega
por inyección (ver :class:`~scorekeeper.core.metrics.base.SingleRubricMetric`).
"""

from __future__ import annotations

from scorekeeper.core.metrics.base import RUBRIC_SLOT, SingleRubricMetric
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Likert


@register
class RazonamientoLogico(SingleRubricMetric):
    name = "razonamiento_logico"
    category = MetricCategory.CALIDAD
    scale = Likert(0.0, 100.0)
    prompts = (
        PromptSlot(
            slug=RUBRIC_SLOT,
            description=(
                "Calidad de la lógica, coherencia de argumentos y justificación de "
                "conclusiones."
            ),
        ),
    )
