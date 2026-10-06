"""Faithfulness — two groundedness metrics for RAG turns.

Both measure whether the assistant's answer is grounded in the retrieved
context, but by different published algorithms. In both, the answer is first
decomposed into atomic, verifiable claims by an LLM extraction step
(``extract_claims``, a ``structured()`` call on ``JudgeStep.EXTRACT``); then each
claim gets one yes/no decision:

* :class:`FaithfulnessRagas` (RAGAS) — verify each claim *against the context* by
  positive entailment. Score is the fraction of claims that can be inferred from the
  context.
* :class:`FaithfulnessDeepeval` (DeepEval) — extract *truths from the context*
  (another LLM step), then per claim ask whether it agrees with the truths or is
  unmentioned. Score is the fraction **not** contradicted — an unverifiable claim (not
  mentioned in the truths) passes; only a direct contradiction fails. Its two other LLM
  steps are pinned (Sonnet for claims and truths extraction, Opus for the per-claim
  verdict).

Every per-claim verdict is a yes/no decision, so it goes through the judge's
``decide()`` seam: with a decision judge in front (TypeSafe's Jev) both verdicts run
there. Each decision reports the model that answered it, and that is what ``judge_model``
names.

DeepEval names Anthropic models explicitly (see the per-call pins below), so it requires
a judge that owns those models — the Anthropic judge or the Claude Agent one; under
another provider the judge raises a Spanish ``ValueError`` for the unowned model. The
pins are nonetheless run through ``judge.resolve_model`` first (a judge may remap them),
so the calls and the reported ``judge_model`` name the model that actually ran. RAGAS
pins nothing: its extraction follows the judge's ``EXTRACT`` routing and its verdicts the
``VERIFY`` one.

Neither metric touches ``retrieved_context`` directly. It is a single Spanish
text blob on ``TurnView``; the judge layer renders it into the prompts that ask
for it with a ``{context}`` placeholder (``generate_truths`` and the RAGAS
``verify``), delimited by ``<contexto></contexto>`` tags, so these metrics stay
agnostic to context shape and just hand the turn to the judge. The DeepEval
``verify`` deliberately omits the placeholder: it judges the claim against the
``{truths}`` already extracted from the context.
All prompts and justification output are Spanish.
"""

from __future__ import annotations

from pydantic import BaseModel

from scorekeeper.core.metrics.base import (
    NOT_APPLICABLE,
    MetricResult,
    MetricTrace,
    MultiStepMetric,
    TraceEntry,
    TraceStep,
    TurnView,
    decision_metadata,
)
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import Judge, JudgeStep
from scorekeeper.core.metrics.prompts import PromptSlot, safe_format
from scorekeeper.core.metrics.registry import register
from scorekeeper.core.metrics.scale import Unit


def _extraction_step(claims: list[str], summary: str = "") -> TraceStep:
    """Trace step recording the extracted claims (as an array, not joined)."""
    summary = summary or (
        f"{len(claims)} afirmación(es) extraída(s) de la respuesta"
        if claims
        else "No se extrajo ninguna afirmación de la respuesta."
    )
    return TraceStep(
        label="Extracción de afirmaciones de la respuesta",
        summary=summary,
        entries=[TraceEntry(label=claim) for claim in claims],
    )


EXTRACT_CLAIMS_SLOT = PromptSlot(
    slug="extract_claims",
    description=(
        "Extracción de afirmaciones atómicas y verificables a partir de la respuesta "
        "del asistente."
    ),
)


def _extract_claims(
    template: str, turn: TurnView, judge: Judge, model: str | None = None
) -> tuple[list[str], TraceStep]:
    """Extract the answer's claims with the judge; return them and their trace step."""
    extraction = judge.structured(
        instruction=template,
        turn=turn,
        schema=Claims,
        step=JudgeStep.EXTRACT,
        model=model,
    )
    claims = [claim for claim in extraction.claims if claim.strip()]
    return claims, _extraction_step(claims, extraction.summary)


# --- Extraction schemas -------------------------------------------------------


class Claims(BaseModel):
    """Atomic, verifiable statements extracted from the assistant's answer."""

    claims: list[str] = []
    summary: str = ""


class Truths(BaseModel):
    """Ground-truth facts extracted from the retrieved context."""

    truths: list[str] = []
    summary: str = ""


# --- Metrics ------------------------------------------------------------------


@register
class FaithfulnessRagas(MultiStepMetric):
    """RAGAS faithfulness: fraction of answer statements entailed by the context."""

    name = "faithfulness_ragas"
    category = MetricCategory.RAG
    scale = Unit()  # 0-1
    weight = 1.0
    prompts = (
        EXTRACT_CLAIMS_SLOT,
        PromptSlot(
            slug="verify",
            required_variables=("claim",),
            description="Veredicto de entailment de una afirmación frente al contexto recuperado.",
        ),
    )

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        claims, extraction_step = _extract_claims(self.prompt("extract_claims"), turn, judge)
        steps = [extraction_step]

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

        # One entailment decision per claim; each is a typed trace entry.
        verify_template = self.prompt("verify")
        verdicts = [
            judge.decide(
                instruction=safe_format(verify_template, claim=claim),
                turn=turn,
                step=JudgeStep.VERIFY,
            )
            for claim in claims
        ]
        steps.append(
            TraceStep(
                label="Verificación de afirmaciones",
                entries=[
                    TraceEntry(
                        label=claim,
                        value=verdict.value,
                        justification=verdict.justification,
                        metadata=decision_metadata(verdict),
                    )
                    for claim, verdict in zip(claims, verdicts, strict=True)
                ],
            )
        )

        # Fraction of statements entailed by the context = supported / n.
        raw = sum(v.value for v in verdicts) / len(verdicts)
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=steps),
            judge_model=verdicts[0].model,
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
        EXTRACT_CLAIMS_SLOT,
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

    # Per-call model pins for the live LLM steps, overridable per instance. All
    # are high-impact, so none runs on a weak model: claims extraction and truths
    # extraction (very high, indirect — any fact dropped here later reads as "no
    # mencionado" and forces a pass) run on Sonnet; the per-claim verdict (direct and dominant — a weak model
    # drifts toward "no verificable", which silently passes) runs on Opus. These are
    # explicit model ids, not JudgeStep routing; both must stay in
    # ``AnthropicJudge.KNOWN_MODELS`` or the judge will reject the call.
    truths_model: str = "claude-sonnet-5"
    verdict_model: str = "claude-opus-4-8"

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        steps: list[TraceStep] = []

        # Extract claims first so the "no claims" case short-circuits before we pay
        # for the truths extraction.
        claims, extraction_step = _extract_claims(
            self.prompt("extract_claims"),
            turn,
            judge,
            model=judge.resolve_model(JudgeStep.EXTRACT, self.truths_model),
        )
        steps.append(extraction_step)
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
        # The template asks whether the claim agrees with the truths or is unmentioned,
        # so true = not contradicted, false = contradicted. No inversion needed.
        verdicts = [
            judge.decide(
                instruction=safe_format(verify_template, truths=truths_text, claim=claim),
                turn=turn,
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
                    value=verdict.value,
                    justification=verdict.justification,
                    metadata=decision_metadata(verdict),
                )
                for claim, verdict in zip(claims, verdicts, strict=True)
            ],
        )
        steps.append(verify_step)

        # true = not contradicted (agrees or unmentioned) = 1, false = contradicted = 0.
        # mean = not_contradicted / n.
        raw = sum(v.value for v in verdicts) / len(verdicts)
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            trace=MetricTrace(steps=steps),
            judge_model=verdicts[0].model,
            rubric_version=self.rubric_version,
        )
