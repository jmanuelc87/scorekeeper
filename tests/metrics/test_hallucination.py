"""Hallucination metric: per-document NLI classification → contradiction rate."""

from __future__ import annotations

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.catalog.hallucination import (
    Hallucination,
    NLIJudgment,
    NLILabel,
)
from scorekeeper.retrieved_context import RetrievedContext


def _turn(context: str) -> TurnView:
    return TurnView(
        prompt="¿Cuál es la política de devoluciones?",
        response="Puedes devolver en 30 días con recibo.",
        retrieved_context=RetrievedContext.from_blob(context),
    )


def test_no_contradictions_is_zero_hallucination(make_judge) -> None:
    judge = make_judge(
        extractions=[
            NLIJudgment(label=NLILabel.ENTAILMENT, justification="Respalda"),
            NLIJudgment(label=NLILabel.NEUTRAL, justification="Ni respalda ni contradice"),
        ]
    )
    result = Hallucination().evaluate(_turn("doc A\n\ndoc B"), judge)

    assert result.raw_score == 0.0  # no contradictions
    # Inverted scale: raw 0.0 hallucination → 1.0 faithfulness (higher-is-better).
    assert result.normalized_score == 1.0
    # One structured (NLI) classification per document, no scoring calls.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]


def test_contradiction_raises_score(make_judge) -> None:
    judge = make_judge(
        extractions=[
            NLIJudgment(label=NLILabel.CONTRADICTION, justification="Contradice el plazo"),
            NLIJudgment(label=NLILabel.ENTAILMENT, justification="Respalda"),
        ]
    )
    result = Hallucination().evaluate(_turn("doc A\n\ndoc B"), judge)

    # 1 of 2 documents contradicts → hallucination 0.5, faithfulness 0.5.
    assert result.raw_score == 0.5
    assert result.normalized_score == 0.5
    assert "1 de 2 documentos contradicen" in result.justification
    assert "### Documento 1: contradiction" in result.justification


def test_no_context_is_zero_without_judge_calls(make_judge) -> None:
    judge = make_judge()
    result = Hallucination().evaluate(_turn(""), judge)

    assert result.raw_score == 0.0
    # No context to contradict → fully faithful (higher-is-better).
    assert result.normalized_score == 1.0
    assert judge.calls == []
    assert "No hay contexto recuperado" in result.justification
