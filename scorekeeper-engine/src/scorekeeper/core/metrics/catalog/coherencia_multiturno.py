"""Coherencia multi-turno.

Consistencia y coherencia de la respuesta a lo largo de múltiples turnos de conversación.

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
class CoherenciaMultiturno(SingleRubricMetric):
    name = "coherencia_multiturno"
    category = MetricCategory.CALIDAD
    scale = Likert(0.0, 100.0)
    prompts = (
        PromptSlot(
            slug=RUBRIC_SLOT,
            description=(
                "Consistencia y coherencia de la respuesta a lo largo de múltiples turnos "
                "de conversación."
            ),
        ),
    )
