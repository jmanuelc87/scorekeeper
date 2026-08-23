"""Precisión.

Exactitud y corrección de los hechos, datos e información proporcionada.

Una sola rúbrica española de 0.0 a 1.0 y una sola llamada al juez, así que la clase
es pura declaración: el texto de la rúbrica vive en ``prompt_versions`` y llega
por inyección (ver :class:`~scorekeeper.core.metrics.base.SingleRubricMetric`).
"""

from __future__ import annotations

from scorekeeper.core.metrics.base import RUBRIC_SLOT, SingleRubricMetric
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Unit


@register
class Precision(SingleRubricMetric):
    name = "precision"
    category = MetricCategory.CALIDAD
    scale = Unit()  # 0-1
    prompts = (
        PromptSlot(
            slug=RUBRIC_SLOT,
            description="Exactitud y corrección de los hechos, datos e información proporcionada.",
        ),
    )
