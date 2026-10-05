"""``Judge`` implementation backed by OpenAI's chat models.

Satisfies the ``Judge`` Protocol structurally (no inheritance). The ``openai`` SDK
is imported lazily — only when the judge builds its own client — so importing this
module never requires the SDK and tests can inject a fake client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    StepModels,
    StructuredDecisions,
    _ScoreResponse,
    clamp,
    judge_call,
    owned_model,
    record_judge_call,
    record_usage,
    render_prompt,
    require_parsed,
    scale_spec,
)

if TYPE_CHECKING:
    from scorekeeper.core.metrics.base import TurnView
    from scorekeeper.core.metrics.scale import Scale

T = TypeVar("T", bound=BaseModel)

PROVIDER = "OpenAI"

# Per-request timeout (seconds) for the provider client; see Settings.judge_timeout_seconds.
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# Chat models this judge may call (exact-match allow-list; extend as models ship).
KNOWN_MODELS = frozenset({"gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"})
# Embedding models this judge may call. Separate from the chat allow-list because
# embeddings run on their own endpoint/model family.
KNOWN_EMBEDDING_MODELS = frozenset(
    {"text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"}
)


class OpenAIJudge(StructuredDecisions):
    """Score turns with an OpenAI chat model via structured (JSON-schema) output.

    ``client`` may be injected (tests); otherwise a real ``openai.OpenAI`` is built
    from ``api_key`` on first construction. Uses the SDK's ``chat.completions.parse``
    structured-outputs helper. Also serves as the project's embeddings backend
    (``embed()``) via the OpenAI embeddings endpoint.
    """

    # Provider name used in error messages. A class attribute so a subclass pointed
    # at a different (OpenAI-compatible) backend reports its own name instead of
    # "OpenAI" in the descriptive errors raised by the inherited call methods.
    provider: str = PROVIDER

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        system_prompt: str | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        step_models: StepModels | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.embedding_model = embedding_model
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        # Route each JudgeStep to a model. With no overrides every step resolves to
        # ``model``, so the judge behaves exactly as a single-model judge.
        self._step_models = step_models or StepModels(model)
        if client is None:
            import openai  # lazy: only needed when building a real client

            # max_retries=0: retrying is owned by ``judge_call``, which backs off with
            # jitter and honors Retry-After. Leaving the SDK's own retries on would
            # multiply the two budgets and make the total wait impossible to reason about.
            # base_url=None keeps the SDK default (https://api.openai.com/v1); set it to
            # target an OpenAI-compatible endpoint, e.g. a local embeddings server.
            client = openai.OpenAI(
                base_url=base_url, api_key=api_key, max_retries=0, timeout=timeout
            )
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
            self._step_models.for_step(step), owns=self._owns, provider=self.provider
        )

    def resolve_model(self, step: JudgeStep | None, model: str | None) -> str:
        """An explicit ``model`` (validated) wins over ``step`` routing."""
        if model is not None:
            return owned_model(model, owns=self._owns, provider=self.provider)
        return self.model_for(step)

    @staticmethod
    def _record_chat_usage(completion: Any) -> None:
        """Record a chat completion's token usage on the active accumulator.

        ``getattr``-safe: a response (or a test fake) without ``usage`` records
        nothing. OpenAI reports ``prompt_tokens``/``completion_tokens``, mapped to
        input/output.
        """
        usage = getattr(completion, "usage", None)
        record_usage(
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

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
        model = self.resolve_model(step, model)
        content = f"{render_prompt(rubric, turn)}\n\n{spec.instruction_es}"

        def _call() -> tuple[Any, Any]:
            # Reading choices[0] inside the retried operation keeps an empty-choices
            # IndexError wrapped as a JudgeError, exactly as the old ``with`` block did.
            completion = self._client.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": content},
                ],
                response_format=_ScoreResponse,
            )
            return completion, completion.choices[0].message

        with record_judge_call(
            step=step, model=model, system_prompt=self.system_prompt, prompt=content
        ):
            completion, message = judge_call(
                _call, provider=self.provider, model=model, action="la puntuación"
            )
            # Recorded before the parse check: the tokens were spent even if the model
            # refused or returned output that does not satisfy the schema; inside the
            # block so they also land on this call's record.
            self._record_chat_usage(completion)
        parsed: _ScoreResponse = require_parsed(
            message.parsed,
            provider=self.provider,
            model=model,
            action="la puntuación",
            refusal=getattr(message, "refusal", None),
        )
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
        model = self.resolve_model(step, model)
        content = render_prompt(instruction, turn)

        def _call() -> tuple[Any, Any]:
            completion = self._client.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": content},
                ],
                response_format=schema,
            )
            return completion, completion.choices[0].message

        with record_judge_call(
            step=step, model=model, system_prompt=self.system_prompt, prompt=content
        ):
            completion, message = judge_call(
                _call, provider=self.provider, model=model, action="la extracción"
            )
            self._record_chat_usage(completion)
        return require_parsed(
            message.parsed,
            provider=self.provider,
            model=model,
            action="la extracción",
            refusal=getattr(message, "refusal", None),
        )

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` via the OpenAI embeddings endpoint, order preserved.

        An explicit ``model`` overrides ``self.embedding_model`` for this call and is
        validated against the embedding allow-list; omitted → the configured default.
        """
        if not texts:
            return []
        embedding_model = (
            owned_model(model, owns=self._owns_embedding, provider=self.provider)
            if model is not None
            else self.embedding_model
        )
        response = judge_call(
            lambda: self._client.embeddings.create(model=embedding_model, input=texts),
            provider=self.provider,
            model=embedding_model,
            action="el cálculo de embeddings",
        )
        # Embeddings usage carries only prompt_tokens (no completion side).
        usage = getattr(response, "usage", None)
        record_usage(input_tokens=getattr(usage, "prompt_tokens", None), output_tokens=None)
        return [item.embedding for item in response.data]
