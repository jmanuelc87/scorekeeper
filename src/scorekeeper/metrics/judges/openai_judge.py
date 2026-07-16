"""``Judge`` implementation backed by OpenAI's chat models.

Satisfies the ``Judge`` Protocol structurally (no inheritance). The ``openai`` SDK
is imported lazily — only when the judge builds its own client — so importing this
module never requires the SDK and tests can inject a fake client.
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

DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class OpenAIJudge:
    """Score turns with an OpenAI chat model via structured (JSON-schema) output.

    ``client`` may be injected (tests); otherwise a real ``openai.OpenAI`` is built
    from ``api_key`` on first construction. Uses the SDK's ``chat.completions.parse``
    structured-outputs helper. Also serves as the project's embeddings backend
    (``embed()``) via the OpenAI embeddings endpoint.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        client: Any | None = None,
        system_prompt: str | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.model = model
        self.embedding_model = embedding_model
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        if client is None:
            import openai  # lazy: only needed when building a real client

            client = openai.OpenAI(api_key=api_key)
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
        completion = self._client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": content},
            ],
            response_format=_ScoreResponse,
        )
        parsed: _ScoreResponse = completion.choices[0].message.parsed
        return JudgeVerdict(
            score=clamp(parsed.score, spec),
            justification=parsed.justification,
            model=self.model,
        )

    def structured(self, *, instruction: str, turn: TurnView, schema: type[T]) -> T:
        completion = self._client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": render_prompt(instruction, turn)},
            ],
            response_format=schema,
        )
        return completion.choices[0].message.parsed

    def embed(self, *, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` via the OpenAI embeddings endpoint, order preserved."""
        if not texts:
            return []
        response = self._client.embeddings.create(model=self.embedding_model, input=texts)
        return [item.embedding for item in response.data]
