"""Hallucination metric: per-document NLI classification → contradiction rate."""

from __future__ import annotations

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.catalog.hallucination import (
    Hallucination,
    NLIJudgment,
    NLILabel,
    split_context_docs,
)


def _turn(context: str) -> TurnView:
    return TurnView(
        prompt="¿Cuál es la política de devoluciones?",
        response="Puedes devolver en 30 días con recibo.",
        retrieved_context=context,
    )


def test_split_context_docs_on_blank_lines() -> None:
    assert split_context_docs("doc uno\ncon dos líneas\n\ndoc dos") == [
        "doc uno\ncon dos líneas",
        "doc dos",
    ]
    assert split_context_docs("  \n  ") == []
    assert split_context_docs("único documento") == ["único documento"]


def test_no_contradictions_is_zero_hallucination(make_judge) -> None:
    judge = make_judge(
        extractions=[
            NLIJudgment(label=NLILabel.ENTAILMENT, justification="Respalda"),
            NLIJudgment(label=NLILabel.NEUTRAL, justification="Ni respalda ni contradice"),
        ]
    )
    result = Hallucination().evaluate(_turn("doc A\n\ndoc B"), judge)

    assert result.raw_score == 0.0  # no contradictions
    assert result.normalized_score == 0.0
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

    # 1 of 2 documents contradicts → hallucination 0.5.
    assert result.raw_score == 0.5
    assert "1 de 2 documentos contradicen" in result.justification
    assert "### Documento 1: contradiction" in result.justification


def test_no_context_is_zero_without_judge_calls(make_judge) -> None:
    judge = make_judge()
    result = Hallucination().evaluate(_turn(""), judge)

    assert result.raw_score == 0.0
    assert judge.calls == []
    assert "No hay contexto recuperado" in result.justification
