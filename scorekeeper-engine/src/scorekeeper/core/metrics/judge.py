"""The Judge seam.

Metrics call a ``Judge`` to run a Spanish rubric prompt (or a non-scoring
extraction/classification step) and get a structured result back. It is a
``Protocol`` so metrics never import an LLM SDK: the real ``AnthropicJudge``
lives in ``judges/`` and is added when the ``anthropic`` dependency lands, while
tests inject a stub that satisfies the same interface.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from scorekeeper.core.metrics.base import TurnView
    from scorekeeper.core.metrics.scale import Scale


class JudgeStep(str, Enum):
    """The role a single model call plays inside a metric's evaluation.

    A metric is a sequence of steps of different kinds, and each kind can be
    routed to a different model — cheap, high-volume work (extraction, per-claim
    verification) on a fast model and the decisive rubric scoring on a stronger
    one. A metric labels each judge call with its ``JudgeStep``; the judge maps
    that step to a concrete model (see ``judges.base.StepModels``), falling back to
    its default model when the step is unmapped or ``None`` — so a judge configured
    with no overrides behaves exactly as a single-model judge and callers that omit
    ``step`` (e.g. tests) are unaffected.

    The members mirror the seam methods that carry them:

    * ``EXTRACT`` — non-scoring extraction/classification (:meth:`Judge.structured`):
      truths from context, NLI labels, node relevance, reverse-generated questions.
    * ``VERIFY`` — per-item boolean checks looped inside a multi-step metric
      (:meth:`Judge.score` on a ``Boolean`` scale), e.g. faithfulness claim checks.
    * ``SCORE`` — the primary/decisive rubric score (:meth:`Judge.score`), as used
      by every :class:`~scorekeeper.core.metrics.base.SingleRubricMetric`.
    * ``EMBED`` — the embedding step (:meth:`Judge.embed`); embeddings already run
      on their own model, so this is here for completeness/observability.
    """

    EXTRACT = "extract"
    VERIFY = "verify"
    SCORE = "score"
    EMBED = "embed"


class JudgeVerdict(BaseModel):
    """The result of a single rubric-scoring judge call."""

    score: float
    justification: str  # Spanish rationale
    model: str | None = None


T = TypeVar("T", bound=BaseModel)


class Judge(Protocol):
    """The interface a metric depends on to reach the LLM-as-a-judge."""

    def model_for(self, step: JudgeStep | None = None) -> str:
        """Resolve the model this judge would use for ``step``.

        Wraps the judge's per-step routing and validates the result against the
        judge's provider. Metrics call it to select a model explicitly and pass it
        back in as ``model=`` — e.g. ``judge.score(..., model=judge.model_for(step))``.
        Raises a Spanish ``ValueError`` when the resolved model does not belong to
        the judge's provider.
        """
        ...

    def resolve_model(self, step: JudgeStep | None, model: str | None) -> str:
        """Resolve the model a call with ``step``/``model`` would actually run on.

        Same precedence as :meth:`score`/:meth:`structured` — an explicit ``model``
        wins over ``step`` routing — but a judge may remap it (e.g. a backend that
        serves a single model). Metrics that pin models call it to report the model
        that really ran instead of the id they asked for.
        """
        ...

    def score(
        self,
        *,
        rubric: str,
        turn: TurnView,
        scale: Scale,
        rubric_version: str | None = None,
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> JudgeVerdict:
        """Score ``turn`` against a Spanish ``rubric`` on ``scale``.

        An explicit ``model`` is used verbatim and wins over ``step`` routing; when
        omitted, ``step`` selects the model (``None`` → the judge's default). Either
        way the model must belong to the judge's provider or a Spanish ``ValueError``
        is raised.
        """
        ...

    def structured(
        self,
        *,
        instruction: str,
        turn: TurnView,
        schema: type[T],
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> T:
        """Run a non-scoring step (extraction/classification) returning ``schema``.

        An explicit ``model`` is used verbatim and wins over ``step`` routing; when
        omitted, ``step`` selects the model (``None`` → the judge's default). A model
        not belonging to the judge's provider raises a Spanish ``ValueError``.
        """
        ...

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed each text, returning one vector per input in the same order.

        Used by similarity-based metrics (e.g. answer relevance). Kept on the same
        seam so metrics stay SDK-free; a judge whose provider offers no embedding
        endpoint may delegate to a configured backend or raise. An explicit ``model``
        overrides the judge's default embedding model and must belong to the
        provider's embedding models.
        """
        ...
