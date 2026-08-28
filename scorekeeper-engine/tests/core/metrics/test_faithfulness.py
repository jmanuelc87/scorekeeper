"""Behavior of the two Faithfulness metrics (RAGAS + DeepEval).

No DB, no live LLM: the StubJudge (from conftest, via ``make_judge``) returns
scripted extractions/verdicts and records call order. Claims are now derived
deterministically from the answer with syntok (one sentence = one claim), so the
judge is only consulted for per-claim verification (and, for DeepEval, the truths
extraction). The metrics are instantiated directly rather than through the
registry.
"""

from __future__ import annotations

from scorekeeper.core.metrics.base import NOT_APPLICABLE, TurnView
from scorekeeper.core.metrics.catalog.faithfulness import (
    FaithfulnessDeepeval,
    FaithfulnessRagas,
    RagasEntailment,
    Truths,
)
from scorekeeper.core.metrics.judge import JudgeVerdict
from seeded_prompts import build


# --- RAGAS --------------------------------------------------------------------

# The RAGAS verdict is a Haiku→Opus cascade run through judge.structured():
# each claim gets one RagasEntailment from the bulk model, and only a
# low-confidence verdict triggers a second (audit) structured call.


def test_ragas_all_supported_is_one(make_judge) -> None:
    # Two sentences → two claims, both entailed with high confidence (no escalation).
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="El router se reinicia en 10s. El LED parpadea.",
    )
    judge = make_judge(
        extractions=[
            RagasEntailment(entailed=True, confidence=1.0, justification="Se deduce"),
            RagasEntailment(entailed=True, confidence=1.0, justification="Se deduce"),
        ],
    )
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    assert result.metric_name == "faithfulness_ragas"
    assert result.raw_score == 1.0
    assert result.normalized_score == 1.0
    # One bulk verdict per claim, all confident → only the bulk model ran.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    assert judge.models == [FaithfulnessRagas.bulk_model, FaithfulnessRagas.bulk_model]
    assert result.judge_model == FaithfulnessRagas.bulk_model


def test_ragas_mixed_is_fraction_supported(make_judge) -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Afirmación fundada aquí. Afirmación inventada aquí.",
    )
    judge = make_judge(
        extractions=[
            RagasEntailment(
                entailed=True, confidence=0.9, justification="Se deduce del contexto"
            ),
            RagasEntailment(
                entailed=False, confidence=0.9, justification="No aparece en el contexto"
            ),
        ],
    )
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    # supported / n = 1 / 2
    assert result.raw_score == 0.5
    # Claims come from syntok (no extraction call); one confident bulk verdict each.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    # Structured trace: an extraction step (claims as entries) + a verification step
    # (one typed entry per claim), no string joining.
    assert len(result.trace.steps) == 2
    extraction, verify = result.trace.steps
    assert extraction.label == "Extracción de afirmaciones de la respuesta"
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


def test_ragas_low_confidence_escalates_to_audit(make_judge) -> None:
    # One claim: the bulk model is unsure (confidence below threshold), so the claim
    # is re-judged by the audit model and the audit verdict wins.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="El router se reinicia en 10s.",
    )
    judge = make_judge(
        extractions=[
            RagasEntailment(entailed=False, confidence=0.3, justification="Dudoso"),
            RagasEntailment(entailed=True, confidence=1.0, justification="Confirmado"),
        ],
    )
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    # Audit verdict (entailed) wins → supported / n = 1 / 1.
    assert result.raw_score == 1.0
    # Two structured calls for the single claim: bulk (Haiku) then audit (Opus).
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    assert judge.models == [FaithfulnessRagas.bulk_model, FaithfulnessRagas.audit_model]
    assert result.judge_model == (
        f"{FaithfulnessRagas.bulk_model} → {FaithfulnessRagas.audit_model}"
    )
    # The escalation is flagged in typed metadata, and the surfaced rationale is
    # the audit model's — no glued-in "(escalado a …)" string.
    assert len(result.trace.steps) == 2  # extraction step + verification step
    entry = result.trace.steps[1].entries[0]
    assert entry.value is True
    assert entry.metadata["escalated"] is True
    assert entry.metadata["model"] == FaithfulnessRagas.audit_model
    assert entry.justification == "Confirmado"


def test_ragas_reports_the_model_the_judge_actually_ran(make_judge) -> None:
    # A judge that remaps every pinned id to a single model: the pins must not leak
    # into the calls, the trace, or judge_model.
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="El router se reinicia en 10s.",
    )
    judge = make_judge(
        extractions=[
            RagasEntailment(entailed=False, confidence=0.3, justification="Dudoso"),
            RagasEntailment(entailed=True, confidence=1.0, justification="Confirmado"),
        ],
        model="local-model",
    )
    judge.resolve_model = lambda step=None, model=None: "local-model"

    result = build(FaithfulnessRagas).evaluate(turn, judge)

    assert judge.models == ["local-model", "local-model"]
    # Both cascade tiers are the same model → no "a → b" arrow to report.
    assert result.judge_model == "local-model"
    assert result.trace.steps[1].entries[0].metadata["model"] == "local-model"


def test_ragas_no_statements_is_not_applicable(make_judge) -> None:
    # Empty response → syntok yields no sentences → nothing to verify.
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="")
    judge = make_judge()
    result = build(FaithfulnessRagas).evaluate(turn, judge)

    # Nothing to verify is not perfect faithfulness — it is nothing measured.
    assert result.raw_score == NOT_APPLICABLE
    assert result.normalized_score == NOT_APPLICABLE
    assert result.judge_model is None
    # No sentences → the judge is never called at all.
    assert [kind for kind, _ in judge.calls] == []
    assert len(result.trace.steps) == 1


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
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

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
        extractions=[Truths(truths=[], summary="Sin verdades")],
    )
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    assert result.raw_score == 0.0
    assert result.judge_model is None
    # Truths extraction runs, but no per-claim verdict call is made.
    assert [kind for kind, _ in judge.calls] == ["structured"]
    # extraction step + truths step + empty-truths step.
    assert len(result.trace.steps) == 3
    assert result.trace.steps[-1].label == "Verdades vacías"


def test_deepeval_no_claims_skips_truths(make_judge) -> None:
    # Empty response → no claims → truths extraction is skipped entirely.
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="")
    judge = make_judge()
    result = build(FaithfulnessDeepeval).evaluate(turn, judge)

    assert result.raw_score == NOT_APPLICABLE
    assert result.normalized_score == NOT_APPLICABLE
    assert result.judge_model is None
    # No claims → neither truths extraction nor any verdict call is made.
    assert [kind for kind, _ in judge.calls] == []
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
        extractions=[Truths(truths=["Verdad 1"], summary="Una verdad")],
        verdicts=[
            JudgeVerdict(score=1, justification="Concuerda", model=verdict_model),
            JudgeVerdict(score=0, justification="Contradice", model=verdict_model),
        ],
        model="claude-opus-4-8",  # the judge default, deliberately different from pins
    )
    build(FaithfulnessDeepeval).evaluate(turn, judge)

    # Call order is truths extraction (structured), then one verdict (score) per claim;
    # models are the explicit per-call pins, not judge.model_for's default.
    assert [kind for kind, _ in judge.calls] == ["structured", "score", "score"]
    assert judge.models == [truths_model, verdict_model, verdict_model]
