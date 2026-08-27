"""Faithfulness — two groundedness metrics for RAG turns.

Both measure whether the assistant's answer is grounded in the retrieved
context, but by different published algorithms. In both, the answer is
decomposed into claims deterministically with :func:`split_sentences` (the
``syntok`` sentence segmenter) — one sentence of the answer is one claim — rather
than by an LLM extraction call. Only the per-claim verification is left to the
judge:

* :class:`FaithfulnessRagas` (RAGAS) — take the answer's sentences as statements,
  then verify each *against the context* by positive entailment. Score is the
  fraction of statements that can be inferred from the context. The per-claim
  entailment runs a two-tier cascade: a cheap high-volume model (Haiku 4.5)
  decides every claim and reports a confidence, and only low-confidence verdicts
  are escalated to a fixed decisive/audit model (Opus 4.8). The two tiers are
  pinned model ids, not the judge's step routing.
* :class:`FaithfulnessDeepeval` (DeepEval) — extract *truths from the context*
  (still an LLM step) and take the answer's sentences as *claims*, then per claim
  ask whether the truths *contradict* it. Score is the fraction **not**
  contradicted — an unverifiable claim (not mentioned in the truths) passes; only
  a direct contradiction fails. Its two LLM steps are likewise pinned (Sonnet for
  truths extraction, Opus for the per-claim verdict).

Because both metrics name Anthropic models explicitly (see the per-call pins
below), they require a judge that owns those models — the Anthropic judge or the
Claude Agent one; under another provider the judge raises a Spanish ``ValueError``
for the unowned model. The pins are nonetheless run through ``judge.resolve_model``
first (a judge may remap them), so the calls, the trace, and the reported
``judge_model`` all name the model that actually ran instead of the id the metric
asked for.

Neither metric touches ``retrieved_context`` directly. It is a single Spanish
text blob on ``TurnView``; the judge layer renders it into every prompt (via the
``{context}`` placeholder and an appended "Contexto recuperado" section), so
these metrics stay agnostic to context shape and just hand the turn to the judge.
All prompts and justification output are Spanish.
"""

from __future__ import annotations

import syntok.segmenter as segmenter
from pydantic import BaseModel

from scorekeeper.core.metrics.base import (
    NOT_APPLICABLE,
    MetricResult,
    MetricTrace,
    MultiStepMetric,
    TraceEntry,
    TraceStep,
    TurnView,
)
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import Judge, JudgeStep
from scorekeeper.core.metrics.prompts import PromptSlot, safe_format
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Boolean, Unit


def split_sentences(text: str) -> list[str]:
    """Segment a text blob into sentences with syntok (deterministic, no LLM).

    Each sentence's surface text is reconstructed from its tokens
    (``token.spacing + token.value``), trimmed, and empty fragments are dropped.
    Empty or whitespace-only input yields an empty list.
    """
    sentences: list[str] = []
    for paragraph in segmenter.analyze(text):
        for sentence in paragraph:
            rebuilt = "".join(token.spacing + token.value for token in sentence).strip()
            if rebuilt:
                sentences.append(rebuilt)
    return sentences


def _extraction_step(claims: list[str]) -> TraceStep:
    """Trace step recording the sentences taken as claims (as an array, not joined)."""
    summary = (
        f"{len(claims)} afirmación(es) extraída(s) de la respuesta"
        if claims
        else "No se extrajo ninguna afirmación de la respuesta."
    )
    return TraceStep(
        label="Extracción de afirmaciones de la respuesta",
        summary=summary,
        entries=[TraceEntry(label=claim) for claim in claims],
    )


# --- Extraction schemas -------------------------------------------------------


class Truths(BaseModel):
    """Ground-truth facts extracted from the retrieved context."""

    truths: list[str] = []
    summary: str = ""


class RagasEntailment(BaseModel):
    """One claim's entailment verdict plus the judge's self-reported confidence.

    Returned by the RAGAS per-claim verification. ``confidence`` (0..1) drives the
    Haiku→Opus cascade: a low-confidence bulk verdict is re-judged by the audit
    model. ``justification`` is the Spanish rationale surfaced in the trace.
    """

    entailed: bool = False
    confidence: float = 0.0  # 0..1
    justification: str = ""


# --- Metrics ------------------------------------------------------------------


@register
class FaithfulnessRagas(MultiStepMetric):
    """RAGAS faithfulness: fraction of answer statements entailed by the context."""

    name = "faithfulness_ragas"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0
    prompts = (
        PromptSlot(
            slug="verify",
            required_variables=("claim",),
            description=(
                "Veredicto de entailment de una afirmación frente al contexto "
                "recuperado, con confianza para la cascada Haiku→Opus."
            ),
        ),
    )

    # Per-call model pins for the entailment cascade (per-claim, the n× hot loop),
    # overridable per instance. A cheap high-volume model (bulk, Haiku) decides every
    # claim; only verdicts it reports below ``escalation_confidence`` are re-judged by
    # the decisive/audit model (Opus). These are explicit model ids, not JudgeStep
    # routing, so the two tiers are fixed regardless of the judge's step config; both
    # must stay in ``AnthropicJudge.KNOWN_MODELS`` or the judge will reject the call.
    bulk_model: str = "claude-haiku-4-5-20251001"
    audit_model: str = "claude-opus-4-8"
    escalation_confidence: float = 0.7

    def _verify_claim(
        self, turn: TurnView, judge: Judge, claim: str, bulk_model: str, audit_model: str
    ) -> tuple[bool, str, bool]:
        """Entailment cascade for one claim.

        The cheap bulk model decides first; if it reports confidence below
        ``self.escalation_confidence`` the claim is re-judged by the audit model and
        that verdict wins. ``bulk_model``/``audit_model`` are the pins already
        resolved through the judge. Returns ``(entailed, justification, escalated)``
        where ``justification`` is from the model whose verdict is used.
        """
        # Resolved once so both cascade tiers judge the exact same text.
        prompt = safe_format(self.prompt("verify"), claim=claim)
        bulk = judge.structured(
            instruction=prompt,
            turn=turn,
            schema=RagasEntailment,
            step=JudgeStep.VERIFY,
            model=bulk_model,
        )
        if bulk.confidence >= self.escalation_confidence:
            return bulk.entailed, bulk.justification, False
        audit = judge.structured(
            instruction=prompt,
            turn=turn,
            schema=RagasEntailment,
            step=JudgeStep.SCORE,
            model=audit_model,
        )
        return audit.entailed, audit.justification, True

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        steps: list[TraceStep] = []

        # Decompose the answer into claims deterministically: one sentence = one
        # claim (syntok), replacing the former LLM extraction call.
        claims = split_sentences(turn.response)
        steps.append(_extraction_step(claims))

        # Nothing to verify → nothing to measure; the metric does not apply.
        if not claims:
            return MetricResult(
                metric_name=self.name,
                raw_score=NOT_APPLICABLE,
                normalized_score=NOT_APPLICABLE,
                trace=MetricTrace(steps=steps),
                judge_model=None,
                rubric_version=self.rubric_version,
            )

        # Per-claim entailment via the Haiku→Opus cascade. Track which models
        # actually ran so ``judge_model`` reflects the escalations. Each claim is a
        # typed entry; escalation and the deciding model become metadata, not glue.
        # The pins are a request: a judge may remap them, so resolve them through the
        # judge and report what actually ran.
        bulk_model = judge.resolve_model(JudgeStep.VERIFY, self.bulk_model)
        audit_model = judge.resolve_model(JudgeStep.SCORE, self.audit_model)

        supported = 0
        escalated_any = False
        verify_step = TraceStep(label="Verificación de afirmaciones")
        for claim in claims:
            entailed, justification, escalated = self._verify_claim(
                turn, judge, claim, bulk_model, audit_model
            )
            supported += int(entailed)
            escalated_any = escalated_any or escalated
            verify_step.entries.append(
                TraceEntry(
                    label=claim,
                    value=entailed,
                    justification=justification,
                    metadata={
                        "escalated": escalated,
                        "model": audit_model if escalated else bulk_model,
                    },
                )
            )
        steps.append(verify_step)

        # Fraction of statements entailed by the context = supported / n.
        raw = supported / len(claims)
        judge_model = (
            f"{bulk_model} → {audit_model}"
            if escalated_any and audit_model != bulk_model
            else bulk_model
        )
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=steps),
            judge_model=judge_model,
            rubric_version=self.rubric_version,
        )


@register
class FaithfulnessDeepeval(MultiStepMetric):
    """DeepEval faithfulness: fraction of answer claims not contradicted by context."""

    name = "faithfulness_deepeval"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0
    prompts = (
        PromptSlot(
            slug="generate_truths",
            description=(
                "Extracción de verdades atómicas del contexto recuperado, contra las "
                "que se verifica cada afirmación de la respuesta."
            ),
        ),
        PromptSlot(
            slug="verify",
            required_variables=("truths", "claim"),
            description=(
                "Veredicto por afirmación: 0 solo si las verdades la contradicen "
                "directamente, 1 si concuerda o no se menciona."
            ),
        ),
    )

    # Per-call model pins for the two live LLM steps, overridable per instance. Both
    # are high-impact, so neither runs on a weak model: truths extraction (very high,
    # indirect — any fact dropped here later reads as "no mencionado" and forces a
    # pass) runs on Sonnet; the per-claim verdict (direct and dominant — a weak model
    # drifts toward "no verificable", which silently passes) runs on Opus. These are
    # explicit model ids, not JudgeStep routing; both must stay in
    # ``AnthropicJudge.KNOWN_MODELS`` or the judge will reject the call.
    truths_model: str = "claude-sonnet-5"
    verdict_model: str = "claude-opus-4-8"

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        steps: list[TraceStep] = []

        # Split claims first so the "no claims" case short-circuits before we pay
        # for the (still LLM-based) truths extraction. One sentence = one claim.
        claims = split_sentences(turn.response)
        steps.append(_extraction_step(claims))
        if not claims:
            return MetricResult(
                metric_name=self.name,
                raw_score=NOT_APPLICABLE,
                normalized_score=NOT_APPLICABLE,
                trace=MetricTrace(steps=steps),
                judge_model=None,
                rubric_version=self.rubric_version,
            )

        truths = judge.structured(
            instruction=self.prompt("generate_truths"),
            turn=turn,
            schema=Truths,
            step=JudgeStep.EXTRACT,
            model=judge.resolve_model(JudgeStep.EXTRACT, self.truths_model),
        )
        steps.append(
            TraceStep(
                label="Extracción de verdades del contexto",
                summary=truths.summary,
                entries=[TraceEntry(label=truth) for truth in truths.truths],
            )
        )

        # No truths extracted → nothing to verify claims against. We cannot attest
        # groundedness, so fail closed (0.0) rather than pass every claim as
        # "no verificable", which would silently score fabricated answers as perfect.
        if not truths.truths:
            steps.append(
                TraceStep(
                    label="Verdades vacías",
                    summary=(
                        "No se extrajeron verdades del contexto; sin base para "
                        "verificar las afirmaciones. Se asigna 0."
                    ),
                )
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                trace=MetricTrace(steps=steps),
                judge_model=None,
                rubric_version=self.rubric_version,
            )

        truths_text = "\n".join(truths.truths)
        verify_template = self.prompt("verify")
        verdicts = [
            judge.score(
                rubric=safe_format(verify_template, truths=truths_text, claim=claim),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
                step=JudgeStep.VERIFY,
                model=judge.resolve_model(JudgeStep.VERIFY, self.verdict_model),
            )
            for claim in claims
        ]
        verify_step = TraceStep(
            label="Veredicto por afirmación",
            entries=[
                TraceEntry(
                    label=claim,
                    value=bool(verdict.score),
                    justification=verdict.justification,
                )
                for claim, verdict in zip(claims, verdicts, strict=True)
            ],
        )
        steps.append(verify_step)

        # Boolean scale → contradicted=0, otherwise 1; mean = not_contradicted / n.
        raw = sum(v.score for v in verdicts) / len(verdicts)
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=steps),
            judge_model=verdicts[0].model,
            rubric_version=self.rubric_version,
        )
