"""Behavior of the two Faithfulness metrics (RAGAS + DeepEval).

No DB, no live LLM: the StubJudge (from conftest, via ``make_judge``) returns
scripted extractions/verdicts and records call order. The metrics are
instantiated directly rather than through the registry.
"""

from __future__ import annotations

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.catalog.faithfulness import (
    Claims,
    FaithfulnessDeepeval,
    FaithfulnessRagas,
    Truths,
)
from scorekeeper.metrics.judge import JudgeVerdict


# --- RAGAS --------------------------------------------------------------------


def test_ragas_all_supported_is_one(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        extractions=[
            Claims(
                claims=["El router se reinicia en 10s", "El LED parpadea"],
                summary="Dos afirmaciones",
            )
        ],
        verdicts=[
            JudgeVerdict(score=1, justification="Se deduce", model="m"),
            JudgeVerdict(score=1, justification="Se deduce", model="m"),
        ],
    )
    result = FaithfulnessRagas().evaluate(turn, judge)

    assert result.metric_name == "faithfulness_ragas"
    assert result.raw_score == 1.0
    assert result.normalized_score == 1.0
    assert result.judge_model == "m"


def test_ragas_mixed_is_fraction_supported(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        extractions=[
            Claims(
                claims=["Afirmación fundada", "Afirmación inventada"],
                summary="Una fundada, una no",
            )
        ],
        verdicts=[
            JudgeVerdict(score=1, justification="Se deduce del contexto", model="m"),
            JudgeVerdict(score=0, justification="No aparece en el contexto", model="m"),
        ],
    )
    result = FaithfulnessRagas().evaluate(turn, judge)

    # supported / n = 1 / 2
    assert result.raw_score == 0.5
    # Call order: one extraction, then one verify per statement.
    assert [kind for kind, _ in judge.calls] == ["structured", "score", "score"]
    # Steps flattened into the single Spanish justification.
    assert "### Extracción de afirmaciones de la respuesta" in result.justification
    assert "### Verificación: Afirmación fundada" in result.justification
    assert "### Verificación: Afirmación inventada" in result.justification
    assert len(result.trace) == 3


def test_ragas_no_statements_is_one(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        extractions=[Claims(claims=[], summary="Sin afirmaciones")]
    )
    result = FaithfulnessRagas().evaluate(turn, judge)

    assert result.raw_score == 1.0  # nothing to verify
    assert result.judge_model is None
    assert [kind for kind, _ in judge.calls] == ["structured"]
    assert len(result.trace) == 1


# --- DeepEval -----------------------------------------------------------------


def test_deepeval_no_contradiction_is_one(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        extractions=[
            Claims(claims=["Afirmación A"], summary="Una afirmación"),
            Truths(truths=["Verdad 1", "Verdad 2"], summary="Dos verdades"),
        ],
        verdicts=[JudgeVerdict(score=1, justification="Concuerda", model="m")],
    )
    result = FaithfulnessDeepeval().evaluate(turn, judge)

    assert result.metric_name == "faithfulness_deepeval"
    assert result.raw_score == 1.0
    # Claims extracted first, then truths, then one verdict per claim.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured", "score"]


def test_deepeval_one_contradicted_lowers_score(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        extractions=[
            Claims(
                claims=["Concuerda", "Contradice", "No mencionada"],
                summary="Tres afirmaciones",
            ),
            Truths(truths=["Verdad 1"], summary="Una verdad"),
        ],
        verdicts=[
            JudgeVerdict(score=1, justification="Concuerda", model="m"),
            JudgeVerdict(score=0, justification="Contradice directamente", model="m"),
            # "idk"/unverifiable → passes (score 1).
            JudgeVerdict(score=1, justification="No se menciona", model="m"),
        ],
    )
    result = FaithfulnessDeepeval().evaluate(turn, judge)

    # not_contradicted / n = 2 / 3 (agreement and idk both pass; only the direct
    # contradiction fails).
    assert result.raw_score == 2 / 3
    assert "### Veredicto: Contradice" in result.justification
    assert len(result.trace) == 5  # 2 extractions + 3 verdicts


def test_deepeval_no_claims_skips_truths(turn: TurnView, make_judge) -> None:
    judge = make_judge(
        extractions=[Claims(claims=[], summary="Sin afirmaciones")]
    )
    result = FaithfulnessDeepeval().evaluate(turn, judge)

    assert result.raw_score == 1.0
    assert result.judge_model is None
    # Truths extraction is skipped when there are no claims.
    assert [kind for kind, _ in judge.calls] == ["structured"]
    assert len(result.trace) == 1
