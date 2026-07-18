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
below), they require the Anthropic judge — under another provider the judge raises
a Spanish ``ValueError`` for the unowned model.

Neither metric touches ``retrieved_context`` directly. It is a single Spanish
text blob on ``TurnView``; the judge layer renders it into every prompt (via the
``{context}`` placeholder and an appended "Contexto recuperado" section), so
these metrics stay agnostic to context shape and just hand the turn to the judge.
All prompts and justification output are Spanish.
"""

from __future__ import annotations

import syntok.segmenter as segmenter
from pydantic import BaseModel

from scorekeeper.metrics.base import (
    MetricResult,
    MultiStepMetric,
    StepTrace,
    TurnView,
)
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge, JudgeStep
from scorekeeper.metrics.registry import register
from scorekeeper.metrics.scale import Boolean, Unit


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


def _describe_claims(claims: list[str]) -> str:
    """Spanish trace detail listing the sentences taken as claims."""
    if not claims:
        return "No se extrajo ninguna afirmación de la respuesta."
    listado = "\n".join(f"- {claim}" for claim in claims)
    return f"{len(claims)} afirmación(es) extraída(s) de la respuesta:\n{listado}"


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


# --- Spanish prompts ----------------------------------------------------------

# Extraction instructions may reference {prompt}/{response}/{context}; the judge
# fills them.
GENERATE_TRUTHS = (
    "Extrae las verdades o hechos presentes en el contexto recuperado. Cada "
    "verdad debe ser un enunciado atómico y verificable tomado únicamente del "
    "contexto. Devuelve también un breve resumen en español.\n"
    "Contexto: {context}"
)

# Verification rubrics: {claim}/{truths} are pre-filled in the metric with
# .format(); they must NOT contain {prompt}/{response}/{context} — the judge
# appends the full turn (including retrieved context) automatically.
# RAGAS entailment is run via judge.structured() (returning RagasEntailment), so the
# prompt must be self-describing: it elicits the verdict, a calibrated confidence and a
# justification directly (structured() does not append a scale instruction the way
# score() does).
VERIFY_RAGAS = (
    "¿Puede inferirse la siguiente afirmación a partir del contexto recuperado?\n"
    "Devuelve:\n"
    "- entailed: true si la afirmación se deduce del contexto, false si no se "
    "deduce o lo contradice.\n"
    "- confidence: tu confianza en ese veredicto, un número entre 0.0 y 1.0 "
    "(1.0 = certeza total; usa valores bajos si el contexto es ambiguo o "
    "insuficiente).\n"
    "- justification: una justificación breve en español.\n"
    "Afirmación: {claim}"
)

VERIFY_DEEPEVAL = (
    "¿Las siguientes verdades contradicen la afirmación? Asigna 0 SOLO si las "
    "verdades contradicen directamente la afirmación. Asigna 1 si la afirmación "
    "concuerda con las verdades o si no se menciona (no verificable). Justifica "
    "brevemente en español.\n"
    "Verdades:\n{truths}\n"
    "Afirmación: {claim}"
)


# --- Metrics ------------------------------------------------------------------


@register(scenarios=["document_retrieval", "web_search"])
class FaithfulnessRagas(MultiStepMetric):
    """RAGAS faithfulness: fraction of answer statements entailed by the context."""

    name = "faithfulness_ragas"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

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
        self, turn: TurnView, judge: Judge, claim: str
    ) -> tuple[bool, str, bool]:
        """Entailment cascade for one claim.

        The cheap bulk model (Haiku) decides first; if it reports confidence below
        ``self.escalation_confidence`` the claim is re-judged by the audit model
        (Opus) and that verdict wins. Returns ``(entailed, justification,
        escalated)`` where ``justification`` is from the model whose verdict is used.
        """
        prompt = VERIFY_RAGAS.format(claim=claim)
        bulk = judge.structured(
            instruction=prompt,
            turn=turn,
            schema=RagasEntailment,
            step=JudgeStep.VERIFY,
            model=self.bulk_model,
        )
        if bulk.confidence >= self.escalation_confidence:
            return bulk.entailed, bulk.justification, False
        audit = judge.structured(
            instruction=prompt,
            turn=turn,
            schema=RagasEntailment,
            step=JudgeStep.SCORE,
            model=self.audit_model,
        )
        return audit.entailed, audit.justification, True

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []

        # Decompose the answer into claims deterministically: one sentence = one
        # claim (syntok), replacing the former LLM extraction call.
        claims = split_sentences(turn.response)
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=_describe_claims(claims),
            )
        )

        # Nothing to verify → nothing can be unfaithful.
        if not claims:
            return MetricResult(
                metric_name=self.name,
                raw_score=1.0,
                normalized_score=self.normalize(1.0),
                justification=self.render_justification(trace),
                judge_model=None,
                rubric_version=self.rubric_version,
                trace=trace,
            )

        # Per-claim entailment via the Haiku→Opus cascade. Track which models
        # actually ran so ``judge_model`` reflects the escalations.
        supported = 0
        escalated_any = False
        for claim in claims:
            entailed, justification, escalated = self._verify_claim(turn, judge, claim)
            supported += int(entailed)
            escalated_any = escalated_any or escalated
            detail = justification
            if escalated:
                detail = f"{justification} (escalado a {self.audit_model})"
            trace.append(StepTrace(label=f"Verificación: {claim}", detail=detail))

        # Fraction of statements entailed by the context = supported / n.
        raw = supported / len(claims)
        judge_model = (
            f"{self.bulk_model} → {self.audit_model}"
            if escalated_any
            else self.bulk_model
        )
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=judge_model,
            rubric_version=self.rubric_version,
            trace=trace,
        )


@register(scenarios=["document_retrieval", "web_search"])
class FaithfulnessDeepeval(MultiStepMetric):
    """DeepEval faithfulness: fraction of answer claims not contradicted by context."""

    name = "faithfulness_deepeval"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0

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
        trace: list[StepTrace] = []

        # Split claims first so the "no claims" case short-circuits before we pay
        # for the (still LLM-based) truths extraction. One sentence = one claim.
        claims = split_sentences(turn.response)
        trace.append(
            StepTrace(
                label="Extracción de afirmaciones de la respuesta",
                detail=_describe_claims(claims),
            )
        )
        if not claims:
            return MetricResult(
                metric_name=self.name,
                raw_score=1.0,
                normalized_score=self.normalize(1.0),
                justification=self.render_justification(trace),
                judge_model=None,
                rubric_version=self.rubric_version,
                trace=trace,
            )

        truths = judge.structured(
            instruction=GENERATE_TRUTHS,
            turn=turn,
            schema=Truths,
            step=JudgeStep.EXTRACT,
            model=self.truths_model,
        )
        trace.append(
            StepTrace(
                label="Extracción de verdades del contexto", detail=truths.summary
            )
        )

        # No truths extracted → nothing to verify claims against. We cannot attest
        # groundedness, so fail closed (0.0) rather than pass every claim as
        # "no verificable", which would silently score fabricated answers as perfect.
        if not truths.truths:
            trace.append(
                StepTrace(
                    label="Verdades vacías",
                    detail=(
                        "No se extrajeron verdades del contexto; sin base para "
                        "verificar las afirmaciones. Se asigna 0."
                    ),
                )
            )
            return MetricResult(
                metric_name=self.name,
                raw_score=0.0,
                normalized_score=self.normalize(0.0),
                justification=self.render_justification(trace),
                judge_model=None,
                rubric_version=self.rubric_version,
                trace=trace,
            )

        truths_text = "\n".join(truths.truths)
        verdicts = [
            judge.score(
                rubric=VERIFY_DEEPEVAL.format(truths=truths_text, claim=claim),
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
                step=JudgeStep.VERIFY,
                model=self.verdict_model,
            )
            for claim in claims
        ]
        for claim, verdict in zip(claims, verdicts, strict=True):
            trace.append(
                StepTrace(label=f"Veredicto: {claim}", detail=verdict.justification)
            )

        # Boolean scale → contradicted=0, otherwise 1; mean = not_contradicted / n.
        raw = sum(v.score for v in verdicts) / len(verdicts)
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=verdicts[0].model,
            rubric_version=self.rubric_version,
            trace=trace,
        )
