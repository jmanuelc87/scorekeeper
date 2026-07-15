"""The Judge seam.

Metrics call a ``Judge`` to run a Spanish rubric prompt (or a non-scoring
extraction/classification step) and get a structured result back. It is a
``Protocol`` so metrics never import an LLM SDK: the real ``AnthropicJudge``
lives in ``judges/`` and is added when the ``anthropic`` dependency lands, while
tests inject a stub that satisfies the same interface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from scorekeeper.metrics.base import TurnView
    from scorekeeper.metrics.scale import Scale


class JudgeVerdict(BaseModel):
    """The result of a single rubric-scoring judge call."""

    score: float
    justification: str  # Spanish rationale
    model: str | None = None


T = TypeVar("T", bound=BaseModel)


class Judge(Protocol):
    """The interface a metric depends on to reach the LLM-as-a-judge."""

    def score(
        self,
        *,
        rubric: str,
        turn: TurnView,
        scale: Scale,
        rubric_version: str | None = None,
    ) -> JudgeVerdict:
        """Score ``turn`` against a Spanish ``rubric`` on ``scale``."""
        ...

    def structured(self, *, instruction: str, turn: TurnView, schema: type[T]) -> T:
        """Run a non-scoring step (extraction/classification) returning ``schema``."""
        ...
