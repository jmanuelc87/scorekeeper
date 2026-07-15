"""Metric-class behavior: single-rubric, normalization, registry.

``MultiStepMetric`` orchestration/trace behavior is covered against the real
catalog metrics in ``test_fidelidad.py``.
"""

from __future__ import annotations

import pytest

from scorekeeper.metrics.base import Metric, SingleRubricMetric, TurnView
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry, register
from scorekeeper.metrics.scale import Boolean, Likert, Unit


class _EjemploLikert(SingleRubricMetric):
    """Minimal single-rubric metric — the catalog has none, so define one here to
    exercise ``SingleRubricMetric.evaluate`` and Likert normalization."""

    name = "_ejemplo_likert"
    category = MetricCategory.RAG
    scale = Likert()  # 1-5
    rubric = "Evalúa (1-5): {prompt} {response}"


def test_single_rubric_maps_verdict_to_result(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        verdicts=[JudgeVerdict(score=4, justification="Correcto en general", model="claude-x")]
    )
    result = _EjemploLikert().evaluate(turn, judge)

    assert result.metric_name == "_ejemplo_likert"
    assert result.raw_score == 4
    assert result.normalized_score == 0.75  # (4-1)/(5-1)
    assert result.judge_model == "claude-x"
    assert result.rubric_version == "v1"
    assert judge.calls == [("score", "v1")]  # exactly one scoring call


def test_normalization_boundaries() -> None:
    assert Likert().normalize(1) == 0.0
    assert Likert().normalize(5) == 1.0
    assert Likert().normalize(4) == 0.75
    assert Unit().normalize(0.3) == 0.3
    assert Boolean().normalize(1) == 1.0
    assert Boolean().normalize(0) == 0.0
    assert Boolean().normalize(0.4) == 0.0


def test_registry_lookup_and_errors(registered_metrics) -> None:
    assert isinstance(MetricRegistry.create("fidelidad_ragas"), Metric)

    with pytest.raises(KeyError, match="Métrica desconocida"):
        MetricRegistry.get("no_existe")

    class Duplicada(SingleRubricMetric):
        name = "fidelidad_ragas"  # already taken

    with pytest.raises(ValueError, match="Métrica duplicada"):
        register(Duplicada)


def test_decorator_declares_scenarios(registered_metrics) -> None:
    @register(scenarios=["soporte_tecnico", "ventas"])
    class Nueva(SingleRubricMetric):
        name = "nueva_metrica"
        scale = Likert()
        rubric = "..."

    assert Nueva.scenarios == ("soporte_tecnico", "ventas")
    assert MetricRegistry.get("nueva_metrica") is Nueva
