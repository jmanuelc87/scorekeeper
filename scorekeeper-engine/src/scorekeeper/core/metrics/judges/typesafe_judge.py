"""``Judge`` decorator that answers binary and multiple-choice decisions with Jev.

TypeSafe's Jev is a decision model, not a text generator: it is asked a typed question
about a piece of state and returns calibrated probabilities over the allowed answers —
a ``Noul`` (yes/no) or a ``Choice`` (one of a fixed set of labels). That is exactly the
shape of the decision steps inside the RAG metrics (node relevance, claim entailment,
claim contradiction, NLI labels), so :class:`TypesafeJudge` takes over
:meth:`~scorekeeper.core.metrics.judge.Judge.decide` and
:meth:`~scorekeeper.core.metrics.judge.Judge.choose` and delegates everything Jev cannot
do — rubric scores with a justification, extractions, embeddings — to the provider judge
it wraps.

Jev gives no justification, so decisions it answers carry an empty one. A ``Noul`` has
no separate confidence; it is derived as ``|2p - 1|``, TypeSafe's own formula, which
puts it on the same scale as a ``Choice``'s confidence. The ``typesafe_sdk`` is imported
lazily, so importing this module never requires it and tests inject a fake client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from scorekeeper.core.metrics.judge import JudgeChoice, JudgeDecision, JudgeStep, JudgeVerdict
from scorekeeper.core.metrics.judges.base import (
    _fill_placeholders,
    judge_call,
    record_judge_call,
    record_usage,
    render_prompt,
    render_turn,
)

if TYPE_CHECKING:
    from scorekeeper.core.metrics.base import TurnView
    from scorekeeper.core.metrics.judge import Judge, T
    from scorekeeper.core.metrics.scale import Scale

PROVIDER = "TypeSafe"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 900.0
# The single question each request carries; answers come back keyed by it.
_QUESTION = "decision"


class TypesafeJudge:
    """Wrap a ``Judge``: decisions go to Jev, every other call to ``inner``."""

    def __init__(
        self,
        inner: Judge,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._inner = inner
        self.model = model
        if client is None:
            from typesafe_sdk import RetryPolicy, TypeSafeClient  # lazy: real client only

            # max_retries=0: retrying is owned by ``judge_call``, as for the other judges.
            client = TypeSafeClient(
                api_key=api_key,
                model=model,
                retry=RetryPolicy(max_retries=0),
                timeout=timeout,
            )
        self._client: Any = client

    def model_for(self, step: JudgeStep | None = None) -> str:
        return self._inner.model_for(step)

    def resolve_model(self, step: JudgeStep | None, model: str | None) -> str:
        return self._inner.resolve_model(step, model)

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
        return self._inner.score(
            rubric=rubric,
            turn=turn,
            scale=scale,
            rubric_version=rubric_version,
            step=step,
            model=model,
        )

    def structured(
        self,
        *,
        instruction: str,
        turn: TurnView,
        schema: type[T],
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> T:
        return self._inner.structured(
            instruction=instruction, turn=turn, schema=schema, step=step, model=model
        )

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        return self._inner.embed(texts=texts, model=model)

    def _ask(
        self,
        question: Any,
        *,
        instruction: str,
        turn: TurnView,
        step: JudgeStep | None,
        action: str,
    ) -> tuple[Any, str]:
        """Ask Jev one ``question`` about ``turn``; return its answer and the model.

        The question carries the filled template as its instructions and the turn is
        the state it is asked about — the same two halves :func:`render_prompt` joins
        for an LLM, which is what the call record stores.
        """
        with record_judge_call(
            step=step,
            model=self.model,
            system_prompt="",
            prompt=render_prompt(instruction, turn),
        ):
            response = judge_call(
                lambda: self._client.system_one(
                    state=render_turn(turn), questions={_QUESTION: question}
                ),
                provider=PROVIDER,
                model=self.model,
                action=action,
            )
            usage = getattr(response, "usage", None)
            record_usage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
        return response.answers[_QUESTION], response.model

    def decide(
        self,
        *,
        instruction: str,
        turn: TurnView,
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> JudgeDecision:
        from typesafe_sdk import Noul

        answer, ran = self._ask(
            Noul(instructions=_fill_placeholders(instruction, turn)),
            instruction=instruction,
            turn=turn,
            step=step,
            action="la decisión binaria",
        )
        return JudgeDecision(
            value=answer.noul >= 0.5, confidence=abs(2 * answer.noul - 1), model=ran
        )

    def choose(
        self,
        *,
        instruction: str,
        turn: TurnView,
        options: Mapping[str, str | None],
        step: JudgeStep | None = None,
        model: str | None = None,
    ) -> JudgeChoice:
        from typesafe_sdk import Choice

        answer, ran = self._ask(
            Choice(instructions=_fill_placeholders(instruction, turn), criteria=dict(options)),
            instruction=instruction,
            turn=turn,
            step=step,
            action="la elección múltiple",
        )
        return JudgeChoice(choice=answer.choice, confidence=answer.confidence, model=ran)
