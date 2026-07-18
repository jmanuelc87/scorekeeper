"""Behavior of the two Faithfulness metrics (RAGAS + DeepEval).

No DB, no live LLM: the StubJudge (from conftest, via ``make_judge``) returns
scripted extractions/verdicts and records call order. Claims are now derived
deterministically from the answer with syntok (one sentence = one claim), so the
judge is only consulted for per-claim verification (and, for DeepEval, the truths
extraction). The metrics are instantiated directly rather than through the
registry.
"""

from __future__ import annotations

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.catalog.faithfulness import (
    FaithfulnessDeepeval,
    FaithfulnessRagas,
    Truths,
)
from scorekeeper.metrics.judge import JudgeVerdict


# --- RAGAS --------------------------------------------------------------------


def test_ragas_all_supported_is_one(make_judge) -> None:
    # Two sentences → two claims, both entailed by the context.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="El router se reinicia en 10s. El LED parpadea.",
    )
    judge = make_judge(
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


def test_ragas_mixed_is_fraction_supported(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación fundada aquí. Afirmación inventada aquí.",
    )
    judge = make_judge(
        verdicts=[
            JudgeVerdict(score=1, justification="Se deduce del contexto", model="m"),
            JudgeVerdict(score=0, justification="No aparece en el contexto", model="m"),
        ],
    )
    result = FaithfulnessRagas().evaluate(turn, judge)

    # supported / n = 1 / 2
    assert result.raw_score == 0.5
    # Claims come from syntok (no extraction call); one verify per sentence.
    assert [kind for kind, _ in judge.calls] == ["score", "score"]
    # Steps flattened into the single Spanish justification.
    assert "### Extracción de afirmaciones de la respuesta" in result.justification
    assert "### Verificación: Afirmación fundada aquí." in result.justification
    assert "### Verificación: Afirmación inventada aquí." in result.justification
    assert len(result.trace) == 3


def test_ragas_no_statements_is_one(make_judge) -> None:
    # Empty response → syntok yields no sentences → nothing to verify.
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="")
    judge = make_judge()
    result = FaithfulnessRagas().evaluate(turn, judge)

    assert result.raw_score == 1.0  # nothing to verify
    assert result.judge_model is None
    # No sentences → the judge is never called at all.
    assert [kind for kind, _ in judge.calls] == []
    assert len(result.trace) == 1


# --- DeepEval -----------------------------------------------------------------


def test_deepeval_no_contradiction_is_one(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación A concuerda con el manual.",
    )
    judge = make_judge(
        extractions=[Truths(truths=["Verdad 1", "Verdad 2"], summary="Dos verdades")],
        verdicts=[JudgeVerdict(score=1, justification="Concuerda", model="m")],
    )
    result = FaithfulnessDeepeval().evaluate(turn, judge)

    assert result.metric_name == "faithfulness_deepeval"
    assert result.raw_score == 1.0
    # Claims from syntok, then the truths extraction, then one verdict per claim.
    assert [kind for kind, _ in judge.calls] == ["structured", "score"]


def test_deepeval_one_contradicted_lowers_score(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Concuerda con todo. Contradice el manual. No se menciona esto.",
    )
    judge = make_judge(
        extractions=[Truths(truths=["Verdad 1"], summary="Una verdad")],
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
    assert "### Veredicto: Contradice el manual." in result.justification
    # 1 claims step + 1 truths step + 3 verdicts.
    assert len(result.trace) == 5


def test_deepeval_empty_truths_is_zero(make_judge) -> None:
    # Non-empty answer, but the context yields no truths → cannot verify → 0.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación sin respaldo en el contexto.",
    )
    judge = make_judge(
        extractions=[Truths(truths=[], summary="Sin verdades")],
    )
    result = FaithfulnessDeepeval().evaluate(turn, judge)

    assert result.raw_score == 0.0
    assert result.judge_model is None
    # Truths extraction runs, but no per-claim verdict call is made.
    assert [kind for kind, _ in judge.calls] == ["structured"]
    assert "### Verdades vacías" in result.justification
    # 1 claims step + 1 truths step + 1 empty-truths step.
    assert len(result.trace) == 3


def test_deepeval_no_claims_skips_truths(make_judge) -> None:
    # Empty response → no claims → truths extraction is skipped entirely.
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="")
    judge = make_judge()
    result = FaithfulnessDeepeval().evaluate(turn, judge)

    assert result.raw_score == 1.0
    assert result.judge_model is None
    # No claims → neither truths extraction nor any verdict call is made.
    assert [kind for kind, _ in judge.calls] == []
    assert len(result.trace) == 1
