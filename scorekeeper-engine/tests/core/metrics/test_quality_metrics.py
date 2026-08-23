"""The ten single-rubric quality metrics: declaration, scale and the shipped rubric.

Their behavior is ``SingleRubricMetric``'s, covered in ``test_metrics.py``; what is
specific to them is the declaration (name, category, 0-1 scale, one ``rubric`` slot) and
that the migration's Spanish text is what reaches the judge.
"""

from __future__ import annotations

import pytest

from scorekeeper.core.metrics.base import RUBRIC_SLOT, SingleRubricMetric, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from seeded_prompts import build, templates_for

QUALITY_METRICS = [
    "relevancia",
    "precision",
    "completitud",
    "claridad",
    "razonamiento_logico",
    "contextualizacion",
    "accionabilidad",
    "estructura",
    "profundidad_analitica",
    "coherencia_multiturno",
]


@pytest.mark.parametrize("name", QUALITY_METRICS)
def test_declaration(name: str) -> None:
    metric_cls = MetricRegistry.get(name)

    assert issubclass(metric_cls, SingleRubricMetric)
    assert metric_cls.category is MetricCategory.CALIDAD
    assert isinstance(metric_cls.scale, Unit)
    # Exactly one slot, and it is the one SingleRubricMetric.evaluate reads.
    assert [slot.slug for slot in metric_cls.prompts] == [RUBRIC_SLOT]
    assert metric_cls.prompts[0].required_variables == ()


@pytest.mark.parametrize("name", QUALITY_METRICS)
def test_scores_a_turn_with_the_seeded_rubric(name: str, turn: TurnView, make_judge) -> None:
    metric = build(MetricRegistry.get(name))
    judge = make_judge(
        verdicts=[JudgeVerdict(score=0.8, justification="Cumple la rúbrica", model="claude-x")]
    )

    result = metric.evaluate(turn, judge)

    assert result.metric_name == name
    assert result.raw_score == 0.8
    assert result.normalized_score == 0.8  # Unit: the raw score is already normalized
    assert judge.steps == [JudgeStep.SCORE]  # one decisive scoring call
    (entry,) = result.trace.steps[0].entries
    assert entry.justification == "Cumple la rúbrica"


@pytest.mark.parametrize("name", QUALITY_METRICS)
def test_seeded_rubric_states_the_unit_scale(name: str) -> None:
    """The judge clamps to the scale, but the rubric is what tells the model the bands."""
    rubric = templates_for(name)[RUBRIC_SLOT]

    assert "Escala 0.0-1.0" in rubric
    assert "- 0.90-1.00:" in rubric and "- 0.00-0.29:" in rubric
