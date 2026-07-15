"""``Judge`` implementation backed by Anthropic's Claude models.

Satisfies the ``Judge`` Protocol structurally (no inheritance), exactly like the
test ``StubJudge``. The ``anthropic`` SDK is imported lazily — only when the judge
has to build its own client — so importing this module never requires the SDK and
tests can inject a fake client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    _ScoreResponse,
    clamp,
    render_prompt,
    scale_spec,
)

if TYPE_CHECKING:
    from scorekeeper.metrics.base import TurnView
    from scorekeeper.metrics.scale import Scale

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicJudge:
    """Score turns with Claude via structured (JSON-schema) output.

    ``client`` may be injected (tests); otherwise a real ``anthropic.Anthropic``
    is built from ``api_key`` on first construction. Adaptive thinking is enabled
    so the model reasons before committing to a score.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        if client is None:
            import anthropic  # lazy: only needed when building a real client

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def score(
        self,
        *,
        rubric: str,
        turn: TurnView,
        scale: Scale,
        rubric_version: str | None = None,
    ) -> JudgeVerdict:
        spec = scale_spec(scale)
        content = f"{render_prompt(rubric, turn)}\n\n{spec.instruction_es}"
        message = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            system=self.system_prompt,
            messages=[{"role": "user", "content": content}],
            output_format=_ScoreResponse,
        )
        parsed: _ScoreResponse = message.parsed_output
        return JudgeVerdict(
            score=clamp(parsed.score, spec),
            justification=parsed.justification,
            model=self.model,
        )

    def structured(self, *, instruction: str, turn: TurnView, schema: type[T]) -> T:
        message = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            system=self.system_prompt,
            messages=[{"role": "user", "content": render_prompt(instruction, turn)}],
            output_format=schema,
        )
        return message.parsed_output
