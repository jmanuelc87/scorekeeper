"""ContextualPrecision: label each retrieved node, then Average Precision.

Tested with no LLM and no DB: the StubJudge scripts one RelevanceVerdict per
node (in rank order). The metric is imported directly from the catalog so these
tests are independent of the isolated ``registered_metrics`` registry.
"""

from __future__ import annotations

import re

import pytest

from scorekeeper.core.metrics.base import NOT_APPLICABLE, TurnView
from scorekeeper.core.metrics.catalog.contextual_precision import (
    ContextualPrecision,
    RelevanceVerdict,
)
from scorekeeper.core.retrieved_context import (
    Chunk,
    RetrievedContext,
    RetrievedDocument,
)
from seeded_prompts import build


def _verdicts(*relevant: bool):
    """Scripted RelevanceVerdict extractions (rank order) for the stub judge."""
    return [
        RelevanceVerdict(relevant=r, justification="relevante" if r else "no relevante")
        for r in relevant
    ]


def _context(blob: str) -> RetrievedContext:
    """Blank-line-separated blocks as content-only documents, one chunk each.

    Stands in for the retired ``RetrievedContext.from_blob``: these tests care about how
    many nodes a judge sees and in what order, not about where the text came from.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", blob)]
    return RetrievedContext(
        documents=[
            RetrievedDocument(name="", document="", chunks=[Chunk(index=0, text=b)])
            for b in blocks
            if b
        ]
    )


def _turn(context: str, *, expected_output: str = "La respuesta correcta.") -> TurnView:
    return TurnView(
        prompt="¿Cuál es la política de devoluciones?",
        response="Puedes devolver en 30 días con recibo.",
        retrieved_context=_context(context),
        expected_output=expected_output,
    )


def test_perfect_ranking_scores_one(make_judge) -> None:
    # Both nodes relevant → AP = (1/1 + 2/2) / 2 = 1.0.
    judge = make_judge(extractions=_verdicts(True, True), model="claude-x")

    result = build(ContextualPrecision).evaluate(_turn("nodo A\n\nnodo B"), judge)

    assert result.metric_name == "contextual_precision"
    assert result.raw_score == pytest.approx(1.0)
    assert result.normalized_score == pytest.approx(1.0)  # Unit scale: identity
    assert result.judge_model == "claude-x"
    # One structured (relevance) classification per node, no scoring calls.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    # Node relevance is a typed entry (value + rank metadata); result step summarizes.
    label_step, result_step = result.trace.steps
    assert label_step.label == "Relevancia por nodo"
    assert label_step.entries[0].label == "Nodo 1"
    assert label_step.entries[0].value is True
    assert label_step.entries[0].metadata["rank"] == 1
    assert result_step.label == "Precisión contextual"


def test_relevant_first_beats_relevant_last(make_judge) -> None:
    # Same set, one relevant + one irrelevant node — order is the whole point.
    # relevant, then irrelevant → (1/1) / 1 = 1.0.
    good_order = build(ContextualPrecision).evaluate(
        _turn("nodo A\n\nnodo B"), make_judge(extractions=_verdicts(True, False))
    )
    # irrelevant, then relevant → (2/2) / 1 = 0.5 (relevant node buried at rank 2).
    bad_order = build(ContextualPrecision).evaluate(
        _turn("nodo A\n\nnodo B"), make_judge(extractions=_verdicts(False, True))
    )

    assert good_order.raw_score == pytest.approx(1.0)
    assert bad_order.raw_score == pytest.approx(0.5)


def test_average_precision_interleaved(make_judge) -> None:
    # verdicts [1, 0, 1]: k1 → 1/1, k2 skipped, k3 → 2/3; AP = (1 + 2/3) / 2.
    judge = make_judge(extractions=_verdicts(True, False, True))

    result = build(ContextualPrecision).evaluate(_turn("a\n\nb\n\nc"), judge)

    assert result.raw_score == pytest.approx((1.0 + 2 / 3) / 2)
    assert "2 de 3 nodos son relevantes" in result.trace.steps[-1].summary


def test_no_relevant_nodes_is_zero(make_judge) -> None:
    judge = make_judge(extractions=_verdicts(False, False))

    result = build(ContextualPrecision).evaluate(_turn("a\n\nb"), judge)

    assert result.raw_score == 0.0
    assert result.normalized_score == 0.0
    # Every node was still labeled before concluding zero.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    assert "Ningún nodo recuperado es relevante" in result.trace.steps[-1].summary


def test_no_context_is_not_applicable_without_judge_calls(make_judge) -> None:
    judge = make_judge()

    result = build(ContextualPrecision).evaluate(_turn(""), judge)

    # No ranking to measure — not a zero-precision retrieval, so it stays out of
    # every average.
    assert result.raw_score == NOT_APPLICABLE
    assert result.normalized_score == NOT_APPLICABLE
    assert judge.calls == []
    assert len(result.trace.steps) == 1
    assert "No hay nodos de contexto recuperado" in result.trace.steps[0].summary


def test_strict_mode_collapses_imperfect_to_zero(make_judge) -> None:
    # AP would be 0.5, but strict_mode passes only a perfect ranking.
    metric = build(ContextualPrecision)
    metric.strict_mode = True

    result = metric.evaluate(
        _turn("a\n\nb"), make_judge(extractions=_verdicts(False, True))
    )

    assert result.raw_score == 0.0


def test_strict_mode_keeps_perfect_score(make_judge) -> None:
    metric = build(ContextualPrecision)
    metric.strict_mode = True

    result = metric.evaluate(
        _turn("a\n\nb"), make_judge(extractions=_verdicts(True, True))
    )

    assert result.raw_score == pytest.approx(1.0)


def test_judges_against_expected_output_not_response(make_judge) -> None:
    turn = _turn("nodo único", expected_output="VERDAD_DE_REFERENCIA")
    judge = make_judge(extractions=_verdicts(True))

    seen: list[tuple[str, str]] = []
    original = judge.structured

    def _spy(*, instruction, turn, schema, step=None, model=None):  # noqa: A002 - mirror protocol kwarg name
        seen.append((instruction, turn.response))
        return original(
            instruction=instruction, turn=turn, schema=schema, step=step, model=model
        )

    judge.structured = _spy
    build(ContextualPrecision).evaluate(turn, judge)

    (instruction, node_response), = seen
    # The ground truth reaches the judge; the assistant's actual answer does not.
    assert "VERDAD_DE_REFERENCIA" in instruction
    assert "Puedes devolver en 30 días" not in instruction
    assert node_response == ""  # the per-node view hides the response
