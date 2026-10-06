"""Behavior of the two Faithfulness metrics (RAGAS + DeepEval).

No DB, no live LLM: the StubJudge (from conftest, via ``make_judge``) returns
scripted extractions/decisions and records call order. Claims come from an LLM
extraction (``structured()`` → ``Claims``) before the per-claim ``decide()`` calls; for
DeepEval a truths extraction follows. The metrics are instantiated directly rather than
through the registry.
"""

from __future__ import annotations

from scorekeeper.core.metrics.base import NOT_APPLICABLE, TurnView
from scorekeeper.core.metrics.catalog.faithfulness import (
    Claims,
    FaithfulnessDeepeval,
    FaithfulnessRagas,
    Truths,
)
from scorekeeper.core.metrics.judge import JudgeDecision
from seeded_prompts import build


# --- RAGAS --------------------------------------------------------------------

# Claims come from one ``structured()`` extraction; each claim then gets exactly one
# ``decide()`` verdict.


def test_ragas_all_supported_is_one(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="El router se reinicia en 10s. El LED parpadea.",
    )
    judge = make_judge(
        extractions=[
            Claims(claims=["El router se reinicia en 10s.", "El LED parpadea."], summary="Dos")
        ],
        decisions=[
            JudgeDecision(value=True, confidence=1.0, justification="Se deduce", model="m"),
            JudgeDecision(value=True, confidence=1.0, justification="Se deduce", model="m"),
        ],
    )
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    assert result.metric_name == "faithfulness_ragas"
    assert result.raw_score == 1.0
    assert result.normalized_score == 1.0
    # Claims extraction, then one verdict per claim — no escalation call.
    assert [kind for kind, _ in judge.calls] == ["structured", "decide", "decide"]
    assert result.judge_model == "m"


def test_ragas_mixed_is_fraction_supported(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación fundada aquí. Afirmación inventada aquí.",
    )
    judge = make_judge(
        extractions=[
            Claims(
                claims=["Afirmación fundada aquí.", "Afirmación inventada aquí."],
                summary="Dos afirmaciones",
            )
        ],
        decisions=[
            JudgeDecision(value=True, confidence=0.9, justification="Se deduce del contexto"),
            JudgeDecision(
                value=False, confidence=0.9, justification="No aparece en el contexto"
            ),
        ],
    )
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    # supported / n = 1 / 2
    assert result.raw_score == 0.5
    assert [kind for kind, _ in judge.calls] == ["structured", "decide", "decide"]
    # Structured trace: an extraction step (claims as entries) + a verification step
    # (one typed entry per claim), no string joining.
    assert len(result.trace.steps) == 2
    extraction, verify = result.trace.steps
    assert extraction.label == "Extracción de afirmaciones de la respuesta"
    assert extraction.summary == "Dos afirmaciones"
    assert [e.label for e in extraction.entries] == [
        "Afirmación fundada aquí.",
        "Afirmación inventada aquí.",
    ]
    assert verify.label == "Verificación de afirmaciones"
    assert [e.label for e in verify.entries] == [
        "Afirmación fundada aquí.",
        "Afirmación inventada aquí.",
    ]
    assert [e.value for e in verify.entries] == [True, False]


def test_ragas_no_statements_is_not_applicable(make_judge) -> None:
    # The extraction yields no claims → nothing to verify.
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="")
    judge = make_judge(extractions=[Claims(claims=[], summary="")])
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    # Nothing to verify is not perfect faithfulness — it is nothing measured.
    assert result.raw_score == NOT_APPLICABLE
    assert result.normalized_score == NOT_APPLICABLE
    assert result.judge_model is None
    # Only the extraction ran; no verdict call.
    assert [kind for kind, _ in judge.calls] == ["structured"]
    assert len(result.trace.steps) == 1


# --- DeepEval -----------------------------------------------------------------


def test_deepeval_no_contradiction_is_one(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación A concuerda con el manual.",
    )
    judge = make_judge(
        extractions=[
            Claims(claims=["Afirmación A concuerda con el manual."], summary="Una"),
            Truths(truths=["Verdad 1", "Verdad 2"], summary="Dos verdades"),
        ],
        decisions=[JudgeDecision(value=True, confidence=1.0, justification="Concuerda", model="m")],
    )
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    assert result.metric_name == "faithfulness_deepeval"
    assert result.raw_score == 1.0
    # Claims extraction, then the truths extraction, then one decision per claim.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured", "decide"]


def test_deepeval_one_contradicted_lowers_score(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Concuerda con todo. Contradice el manual. No se menciona esto.",
    )
    judge = make_judge(
        extractions=[
            Claims(
                claims=["Concuerda con todo.", "Contradice el manual.", "No se menciona esto."],
                summary="Tres",
            ),
            Truths(truths=["Verdad 1"], summary="Una verdad"),
        ],
        decisions=[
            JudgeDecision(value=True, confidence=1.0, justification="Concuerda", model="m"),
            JudgeDecision(value=False, confidence=1.0, justification="Contradice directamente", model="m"),
            # "idk"/unverifiable → not a contradiction, so it passes.
            JudgeDecision(value=True, confidence=1.0, justification="No se menciona", model="m"),
        ],
    )
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    # not_contradicted / n = 2 / 3 (agreement and idk both pass; only the direct
    # contradiction fails).
    assert result.raw_score == 2 / 3
    # extraction step + truths step + verdict step (one typed entry per claim).
    assert len(result.trace.steps) == 3
    verdict_step = result.trace.steps[2]
    assert verdict_step.label == "Veredicto por afirmación"
    contra = next(e for e in verdict_step.entries if e.label == "Contradice el manual.")
    assert contra.value is False
    assert contra.justification == "Contradice directamente"


def test_deepeval_empty_truths_is_zero(make_judge) -> None:
    # Non-empty answer, but the context yields no truths → cannot verify → 0.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación sin respaldo en el contexto.",
    )
    judge = make_judge(
        extractions=[
            Claims(claims=["Afirmación sin respaldo en el contexto."], summary="Una"),
            Truths(truths=[], summary="Sin verdades"),
        ],
    )
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    assert result.raw_score == 0.0
    assert result.judge_model is None
    # Both extractions run, but no per-claim verdict call is made.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    # extraction step + truths step + empty-truths step.
    assert len(result.trace.steps) == 3
    assert result.trace.steps[-1].label == "Verdades vacías"


def test_deepeval_no_claims_skips_truths(make_judge) -> None:
    # Empty response → no claims → truths extraction is skipped entirely.
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="")
    judge = make_judge(extractions=[Claims(claims=[], summary="")])
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    assert result.raw_score == NOT_APPLICABLE
    assert result.normalized_score == NOT_APPLICABLE
    assert result.judge_model is None
    # No claims → truths extraction and verdict calls are skipped.
    assert [kind for kind, _ in judge.calls] == ["structured"]
    assert len(result.trace.steps) == 1


def test_deepeval_pins_models_per_call(make_judge) -> None:
    # The two live LLM calls are pinned per the impact analysis: truths extraction on
    # Sonnet, each per-claim verdict on Opus — regardless of the judge's own default.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Concuerda con el manual. Contradice el manual.",
    )
    verdict_model = FaithfulnessDeepeval.verdict_model
    truths_model = FaithfulnessDeepeval.truths_model
    judge = make_judge(
        extractions=[
            Claims(claims=["Concuerda con el manual.", "Contradice el manual."], summary="Dos"),
            Truths(truths=["Verdad 1"], summary="Una verdad"),
        ],
        decisions=[
            JudgeDecision(value=True, confidence=1.0, justification="Concuerda", model=verdict_model),
            JudgeDecision(value=False, confidence=1.0, justification="Contradice", model=verdict_model),
        ],
        model="claude-opus-4-8",  # the judge default, deliberately different from pins
    )
    build(FaithfulnessDeepeval).evaluate(turn, judge)

    # Call order is claims then truths extraction (structured), then one decision per
    # claim; models are the explicit per-call pins, not judge.model_for's default.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured", "decide", "decide"]
    assert judge.models == [truths_model, truths_model, verdict_model, verdict_model]


def test_ragas_trace_entries_carry_decision_metadata(make_judge) -> None:
    # Decision trace entries must include model, value, and confidence in metadata.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación A. Afirmación B.",
    )
    judge = make_judge(
        extractions=[Claims(claims=["Afirmación A.", "Afirmación B."], summary="Dos")],
        decisions=[
            JudgeDecision(value=True, confidence=0.95, justification="Se deduce", model="jev-latest"),
            JudgeDecision(value=False, confidence=0.85, justification="", model="jev-latest"),
        ],
    )
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    verify_step = result.trace.steps[1]
    assert verify_step.label == "Verificación de afirmaciones"
    # Each decision entry must carry model, value, and confidence in metadata.
    for entry, expected_value, expected_confidence in zip(
        verify_step.entries,
        [True, False],
        [0.95, 0.85],
    ):
        assert entry.metadata["model"] == "jev-latest"
        assert entry.metadata["value"] == expected_value
        assert entry.metadata["confidence"] == expected_confidence


def test_deepeval_trace_entries_carry_decision_metadata(make_judge) -> None:
    # Decision trace entries must include model, value, and confidence in metadata.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Concuerda. Contradice.",
    )
    judge = make_judge(
        extractions=[
            Claims(claims=["Concuerda.", "Contradice."], summary="Dos"),
            Truths(truths=["Verdad 1"], summary="Una"),
        ],
        decisions=[
            JudgeDecision(value=True, confidence=0.9, justification="Concuerda", model="jev-latest"),
            JudgeDecision(value=False, confidence=0.88, justification="Contradice", model="jev-latest"),
        ],
    )
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    verdict_step = result.trace.steps[2]
    assert verdict_step.label == "Veredicto por afirmación"
    # Each decision entry must carry model, value, and confidence in metadata.
    for entry, expected_value, expected_confidence in zip(
        verdict_step.entries,
        [True, False],
        [0.9, 0.88],
    ):
        assert entry.metadata["model"] == "jev-latest"
        assert entry.metadata["value"] == expected_value
        assert entry.metadata["confidence"] == expected_confidence
