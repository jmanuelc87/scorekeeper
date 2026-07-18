"""``Judge`` implementation backed by OpenAI's chat models.

Satisfies the ``Judge`` Protocol structurally (no inheritance). The ``openai`` SDK
is imported lazily — only when the judge builds its own client — so importing this
module never requires the SDK and tests can inject a fake client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from scorekeeper.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    StepModels,
    _ScoreResponse,
    clamp,
    owned_model,
    render_prompt,
    scale_spec,
)

if TYPE_CHECKING:
    from scorekeeper.metrics.base import TurnView
    from scorekeeper.metrics.scale import Scale

T = TypeVar("T", bound=BaseModel)

PROVIDER = "OpenAI"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# Chat models this judge may call (exact-match allow-list; extend as models ship).
KNOWN_MODELS = frozenset({"gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"})
# Embedding models this judge may call. Separate from the chat allow-list because
# embeddings run on their own endpoint/model family.
KNOWN_EMBEDDING_MODELS = frozenset(
    {"text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"}
)


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
        step_models: StepModels | None = None,
    ) -> None:
        self.model = model
        self.embedding_model = embedding_model
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        # Route each JudgeStep to a model. With no overrides every step resolves to
        # ``model``, so the judge behaves exactly as a single-model judge.
        self._step_models = step_models or StepModels(model)
        if client is None:
            import openai  # lazy: only needed when building a real client

            client = openai.OpenAI(api_key=api_key)
        self._client: Any = client

    def _owns(self, model: str) -> bool:
        """Whether ``model`` is an OpenAI chat model this judge may call."""
        return model in KNOWN_MODELS

    def _owns_embedding(self, model: str) -> bool:
        """Whether ``model`` is an OpenAI embedding model this judge may call."""
        return model in KNOWN_EMBEDDING_MODELS

    def model_for(self, step: JudgeStep | None = None) -> str:
        """Resolve (and validate) the chat model for ``step`` via the step router."""
        return owned_model(
            self._step_models.for_step(step), owns=self._owns, provider=PROVIDER
        )

    def _resolve(self, step: JudgeStep | None, model: str | None) -> str:
        """An explicit ``model`` (validated) wins over ``step`` routing."""
        if model is not None:
            return owned_model(model, owns=self._owns, provider=PROVIDER)
        return self.model_for(step)

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
        spec = scale_spec(scale)
        model = self._resolve(step, model)
        content = f"{render_prompt(rubric, turn)}\n\n{spec.instruction_es}"
        completion = self._client.chat.completions.parse(
            model=model,
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
        completion = self._client.chat.completions.parse(
            model=self._resolve(step, model),
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": render_prompt(instruction, turn)},
            ],
            response_format=schema,
        )
        return completion.choices[0].message.parsed

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` via the OpenAI embeddings endpoint, order preserved.

        An explicit ``model`` overrides ``self.embedding_model`` for this call and is
        validated against the embedding allow-list; omitted → the configured default.
        """
        if not texts:
            return []
        embedding_model = (
            owned_model(model, owns=self._owns_embedding, provider=PROVIDER)
            if model is not None
            else self.embedding_model
        )
        response = self._client.embeddings.create(model=embedding_model, input=texts)
        return [item.embedding for item in response.data]
