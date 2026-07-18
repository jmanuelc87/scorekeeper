"""Core metric taxonomy.

A ``Metric`` is *behavior*, not data: scale/weight/category/scenarios are
class-level metadata, but how a score is produced is the polymorphic
``evaluate()`` method. Metrics operate on a decoupled ``TurnView`` — never the
SQLAlchemy ``Turn`` — so the whole package is testable with no database.

Two shapes cover the space:

* ``SingleRubricMetric`` — one Spanish rubric → one judge call → one score. A
  concrete metric is then pure declaration (name/category/scale/weight/rubric).
* ``MultiStepMetric`` — override ``evaluate()`` to orchestrate several judge
  calls (extract → verify → aggregate) and flatten a step trace into the single
  Spanish ``justification`` field.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel

from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge, JudgeStep
from scorekeeper.metrics.scale import Scale


class TurnView(BaseModel):
    """Read-only projection of a Turn handed to metrics (no ORM coupling)."""

    prompt: str
    response: str
    turn_number: int = 1
    # Prior (prompt, response) exchanges, for metrics that need conversation context.
    history: list[tuple[str, str]] = []
    # Retrieved context a RAG answer was grounded on, for groundedness-style metrics.
    retrieved_context: str = ""
    # Ground-truth answer for the turn, for reference-based metrics (e.g. contextual
    # precision judges retrieved nodes against this, not the generator's response).
    expected_output: str = ""


class StepTrace(BaseModel):
    """One recorded step of a multi-step evaluation (Spanish label + detail)."""

    label: str
    detail: str


class MetricResult(BaseModel):
    """A metric's output for one turn — flattened onto a ``MetricScore`` row."""

    metric_name: str
    raw_score: float  # in the metric's own scale
    normalized_score: float  # always [0, 1], used at rollup
    justification: str  # Spanish; multi-step traces are flattened into this
    judge_model: str | None = None
    rubric_version: str | None = None
    # Not persisted — kept for debugging/observability only.
    trace: list[StepTrace] = []


class Metric(ABC):
    """Base class for an evaluation metric.

    Class-level metadata every concrete metric must set (``scale``), or may
    override (``weight``, ``rubric_version``, ``scenarios``). ``scenarios`` lists
    the ``use_case`` values this metric applies to; the ``@register`` decorator
    can populate it. An empty tuple means the metric belongs to the reserved
    ``"default"`` selection only.
    """

    name: ClassVar[str]
    category: ClassVar[MetricCategory]
    scale: ClassVar[Scale]
    weight: ClassVar[float] = 1.0
    rubric_version: ClassVar[str] = "v1"
    scenarios: ClassVar[tuple[str, ...]] = ()

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
            justification=verdict.justification,
            judge_model=verdict.model,
            rubric_version=self.rubric_version,
        )


class MultiStepMetric(Metric):
    """A metric whose score needs several orchestrated steps.

    Subclasses implement ``evaluate()`` and typically build a list of
    ``StepTrace`` entries, then call ``render_justification()`` to flatten them
    into the single Spanish ``justification`` Text field.
    """

    def render_justification(self, trace: list[StepTrace]) -> str:
        return "\n\n".join(f"### {step.label}\n{step.detail}" for step in trace)
