"""Core metric taxonomy.

A ``Metric`` is *behavior*, not data: scale/weight/category are
class-level metadata, but how a score is produced is the polymorphic
``evaluate()`` method. Metrics operate on a decoupled ``TurnView`` — never the
SQLAlchemy ``Turn`` — so the whole package is testable with no database.

Two shapes cover the space:

* ``SingleRubricMetric`` — one Spanish rubric → one judge call → one score. A
  concrete metric is then pure declaration (name/category/scale/weight/rubric).
* ``MultiStepMetric`` — override ``evaluate()`` to orchestrate several judge
  calls (extract → verify → aggregate) and record what each step produced as a
  structured :class:`MetricTrace` (steps → typed entries), so lists stay arrays
  instead of being flattened into a single string.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.judge import Judge, JudgeStep
from scorekeeper.core.metrics.prompts import PromptSlot
from scorekeeper.core.metrics.scale import Scale
from scorekeeper.core.retrieved_context import RetrievedContext


NOT_APPLICABLE = -1.0
"""Sentinel score for a turn the metric could not measure at all.

A metric short-circuits with this when its input is absent (no retrieved context,
no claims, no questions): there is nothing to score, and inventing a ``0.0`` or a
``1.0`` would push a fabricated value into every mean above it. Rollup skips it
instead — see :func:`scorekeeper.core.metrics.rollup.turn_score`.
"""


def is_not_applicable(score: float) -> bool:
    """Whether ``score`` is the not-applicable sentinel.

    Any negative value counts: real scores live in their scale's own range, which
    is never below zero, so the sign alone carries the meaning.
    """
    return score < 0.0


def context_documents(raw: str) -> list[str]:
    """Split a ``retrieved_context`` value into individual retrieved documents.

    Two storage shapes are supported so metrics behave the same whichever ingest
    path produced the turn:

    * The browser extension stores a JSON array of ``{"name", "url"}`` records —
      one array element per retrieved source. Each is rendered as a ``name\\nurl``
      document (just the URL when unnamed).
    * Spreadsheet imports store a free-form text blob, so blank-line separated
      blocks are treated as separate documents (multi-line documents stay intact).

    Empty input yields no documents.
    """
    docs = _documents_from_json(raw)
    if docs is not None:
        return docs
    return [block.strip() for block in re.split(r"\n\s*\n", raw) if block.strip()]


def _documents_from_json(raw: str) -> list[str] | None:
    """Render a JSON citation array into documents, or ``None`` if not that shape.

    Only a JSON *array* counts as the structured shape; anything else (a bare
    string that happens to parse, a spreadsheet blob) falls back to text splitting.
    """
    text = raw.strip()
    if not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None  # A ``[`` that isn't valid JSON — treat as free-form text.
    if not isinstance(parsed, list):
        return None

    docs: list[str] = []
    for item in parsed:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            doc = f"{name}\n{url}" if name and url else name or url
        else:
            doc = str(item).strip()
        if doc:
            docs.append(doc)
    return docs


def context_blob(raw: str) -> str:
    """Render ``retrieved_context`` as the blank-line separated text a judge reads.

    Round-trips a free-form text blob unchanged while turning the extension's JSON
    citation array into the same readable ``name/url`` blocks, so rubrics see one
    consistent shape regardless of how the turn was ingested.
    """
    return "\n\n".join(context_documents(raw))


class TurnView(BaseModel):
    """Read-only projection of a Turn handed to metrics (no ORM coupling)."""

    prompt: str
    response: str
    turn_number: int = 1
    # Prior (prompt, response) exchanges, for metrics that need conversation context.
    history: list[tuple[str, str]] = []
    # Retrieved context a RAG answer was grounded on, for groundedness-style metrics.
    retrieved_context: RetrievedContext = Field(default_factory=RetrievedContext)
    # Ground-truth answer for the turn, for reference-based metrics (e.g. contextual
    # precision judges retrieved nodes against this, not the generator's response).
    expected_output: str = ""


class TraceEntry(BaseModel):
    """One typed observation a step produced (a claim verdict, a similarity, a node judgment).

    ``value`` carries the machine-readable outcome; the union order ``bool | float
    | str`` is intentional — Pydantic v2 matches ``bool`` before ``float`` so a
    boolean verdict is not coerced to ``1.0``. ``metadata`` holds extra typed
    fields that don't fit ``value`` (confidence, model, escalated, rank, …).
    """

    label: str  # Spanish; e.g. the claim / node / question text
    value: bool | float | str | None = None
    justification: str = ""  # Spanish rationale from the judge
    metadata: dict[str, Any] = {}


class TraceStep(BaseModel):
    """One phase of a multi-step evaluation (extraction, per-claim verification…)."""

    label: str  # Spanish phase name
    summary: str | None = None  # optional Spanish one-liner for the phase
    entries: list[TraceEntry] = []  # kept as an ARRAY, never string-joined


class MetricTrace(BaseModel):
    """Structured record of what a metric produced while evaluating a turn.

    Persisted on the ``metric_traces`` table (1:1 with ``MetricScore``, its
    ``steps`` stored as JSON) for direct inspection — not surfaced by the read
    APIs. Replaces the former flattened Spanish ``justification`` string.
    """

    steps: list[TraceStep] = []


class MetricResult(BaseModel):
    """A metric's output for one turn — persisted onto a ``MetricScore`` row."""

    metric_name: str
    raw_score: float  # in the metric's own scale, or NOT_APPLICABLE
    normalized_score: float  # [0, 1] used at rollup, or NOT_APPLICABLE
    trace: MetricTrace = MetricTrace()  # structured record, persisted as JSON
    judge_model: str | None = None
    rubric_version: str | None = None


class Metric(ABC):
    """Base class for an evaluation metric.

    Class-level metadata every concrete metric must set (``scale``), or may
    override (``weight``, ``rubric_version``). Which use cases a metric belongs
    to is *not* declared here: that mapping is user data, composed through
    ``POST /use-cases`` and stored in ``use_case_metrics``.

    ``prompts`` declares the metric's prompt *slots* — the same split one level
    down: the slug and required variables are code, the text is not. Empty by
    default, so a metric with no editable prompt declares nothing.

    The text arrives by **injection**: ``selection.resolve`` reads the active
    ``PromptVersion`` for each slot and passes it to the constructor, and
    ``evaluate`` reads it back with :meth:`prompt`. A metric therefore never
    holds a session and never queries for its own rubric, which is what keeps
    this package importable and unit-testable with no database.
    """

    name: ClassVar[str]
    category: ClassVar[MetricCategory]
    scale: ClassVar[Scale]
    weight: ClassVar[float] = 1.0
    rubric_version: ClassVar[str] = "v1"
    prompts: ClassVar[tuple[PromptSlot, ...]] = ()

    def __init__(self, templates: Mapping[str, str] | None = None) -> None:
        """Bind this instance's prompt templates, keyed by slot slug.

        ``templates`` is optional so a metric declaring no slot still constructs as
        ``Cls()``. A *non-empty* map must cover every declared slot: a partial one is
        a resolution bug, and catching it here — on the event loop, in ``resolve`` —
        beats letting :meth:`prompt` raise later inside a judge worker thread, where
        the runner's skip-metric-continue would swallow it as a warning.
        """
        self._templates = dict(templates or {})
        missing = [slot.slug for slot in self.prompts if slot.slug not in self._templates]
        if self._templates and missing:
            raise ValueError(
                f"La métrica {self.name} no recibió plantilla para: {', '.join(missing)}."
            )

    def prompt(self, slug: str) -> str:
        """The template bound to ``slug``.

        Raises ``LookupError`` when nothing is bound — either the metric was built
        with no templates at all (a test constructing it directly), or it is asking
        for a slug it never declared in ``prompts``.
        """
        try:
            return self._templates[slug]
        except KeyError:
            raise LookupError(
                f"La métrica {self.name} no tiene plantilla asignada para el prompt "
                f"{slug!r}. ¿Se construyó sin plantillas o el slot no está declarado?"
            ) from None

    @abstractmethod
    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        """Produce this metric's score for ``turn`` using ``judge``."""

    def normalize(self, raw: float) -> float:
        return self.scale.normalize(raw)


class SingleRubricMetric(Metric):
    """One Spanish rubric, one judge call. Concrete subclasses just declare."""

    rubric: ClassVar[str]  # Spanish prompt template

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        verdict = judge.score(
            rubric=self.rubric,
            turn=turn,
            scale=self.scale,
            rubric_version=self.rubric_version,
            step=JudgeStep.SCORE,
            model=judge.model_for(JudgeStep.SCORE),
        )
        return MetricResult(
            metric_name=self.name,
            raw_score=verdict.score,
            normalized_score=self.normalize(verdict.score),
            trace=MetricTrace(
                steps=[
                    TraceStep(
                        label="Puntuación",
                        entries=[
                            TraceEntry(
                                label=self.name,
                                value=verdict.score,
                                justification=verdict.justification,
                                metadata=(
                                    {"model": verdict.model} if verdict.model else {}
                                ),
                            )
                        ],
                    )
                ]
            ),
            judge_model=verdict.model,
            rubric_version=self.rubric_version,
        )


class MultiStepMetric(Metric):
    """A metric whose score needs several orchestrated steps.

    Subclasses implement ``evaluate()`` and build a :class:`MetricTrace` — a list
    of ``TraceStep`` phases, each holding typed ``TraceEntry`` observations — so
    the per-item detail (claims, verdicts, similarities) stays structured instead
    of being flattened into a single Spanish string.
    """
