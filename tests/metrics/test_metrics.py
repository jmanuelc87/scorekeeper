"""Metric-class behavior: single-rubric, multi-step, normalization, registry."""

from __future__ import annotations

import pytest

from scorekeeper.metrics.base import Metric, SingleRubricMetric, TurnView
from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry, register
from scorekeeper.metrics.scale import Boolean, Likert, Unit


def test_single_rubric_maps_verdict_to_result(turn: TurnView, make_judge, registered_metrics) -> None:
    judge = make_judge(
        verdicts=[JudgeVerdict(score=4, justification="Correcto en general", model="claude-x")]
    )
    result = MetricRegistry.create("correccion").evaluate(turn, judge)

    assert result.metric_name == "correccion"
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


def test_multistep_orchestration_and_trace(
    turn: TurnView, make_judge, make_extraction, registered_metrics
) -> None:
    judge = make_judge(
        verdicts=[
            JudgeVerdict(score=1, justification="Cierto", model="m"),
            JudgeVerdict(score=0, justification="Falso", model="m"),
        ],
        extractions=[
            make_extraction(
                ["El router se reinicia en 10s", "El LED parpadea"],
                summary="Dos afirmaciones verificables",
            )
        ],
    )
    result = MetricRegistry.create("seguridad_factual").evaluate(turn, judge)

    # Aggregate of two boolean verifications: (1 + 0) / 2.
    assert result.raw_score == 0.5
    assert result.normalized_score == 0.5
    # Call order/count: extract, then verify each claim.
    assert [kind for kind, _ in judge.calls] == ["structured", "score", "score"]
    # Multi-step reasoning flattened into the single Spanish justification.
    assert "### Extracción de afirmaciones" in result.justification
    assert "### Verificación: El router se reinicia en 10s" in result.justification
    assert "### Verificación: El LED parpadea" in result.justification
    assert len(result.trace) == 3


def test_multistep_no_claims_is_safe(
    turn: TurnView, make_judge, make_extraction, registered_metrics
) -> None:
    judge = make_judge(extractions=[make_extraction([], summary="Sin afirmaciones")])
    result = MetricRegistry.create("seguridad_factual").evaluate(turn, judge)

    assert result.raw_score == 1.0  # nothing to be wrong about
    assert [kind for kind, _ in judge.calls] == ["structured"]


def test_registry_lookup_and_errors(registered_metrics) -> None:
    assert isinstance(MetricRegistry.create("tono"), Metric)

    with pytest.raises(KeyError, match="Métrica desconocida"):
        MetricRegistry.get("no_existe")

    class Duplicada(SingleRubricMetric):
        name = "correccion"  # already taken

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
