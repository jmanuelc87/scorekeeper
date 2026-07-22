"""``Judge`` implementation backed by Anthropic's Claude models.

Satisfies the ``Judge`` Protocol structurally (no inheritance), exactly like the
test ``StubJudge``. The ``anthropic`` SDK is imported lazily — only when the judge
has to build its own client — so importing this module never requires the SDK and
tests can inject a fake client.
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
    record_usage,
    render_prompt,
    scale_spec,
)

if TYPE_CHECKING:
    from scorekeeper.metrics.base import TurnView
    from scorekeeper.metrics.scale import Scale

T = TypeVar("T", bound=BaseModel)

PROVIDER = "Anthropic"
DEFAULT_MODEL = "claude-opus-4-8"
# Models this judge is allowed to call. An exact-match allow-list (the provider's
# model space is small and Anthropic-owned); extend it as new Claude models ship.
KNOWN_MODELS = frozenset(
    {"claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"}
)
# Models that support adaptive thinking (a 4.6+ feature). Others — e.g.
# Haiku 4.5 — reject ``thinking={"type": "adaptive"}`` with a 400, so those calls
# omit the ``thinking`` parameter entirely (no thinking). Keep in sync as new
# adaptive-capable models are added to KNOWN_MODELS.
ADAPTIVE_THINKING_MODELS = frozenset({"claude-opus-4-8", "claude-sonnet-5"})


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
        embedder: Any | None = None,
        step_models: StepModels | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        # Route each JudgeStep to a model. With no overrides every step resolves to
        # ``model``, so the judge behaves exactly as a single-model judge.
        self._step_models = step_models or StepModels(model)
        # Anthropic offers no embeddings endpoint; embedding metrics delegate to
        # this backend (any object with an ``embed()`` method, e.g. an OpenAIJudge).
        self._embedder = embedder
        if client is None:
            import anthropic  # lazy: only needed when building a real client

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def _owns(self, model: str) -> bool:
        """Whether ``model`` is an Anthropic model this judge may call."""
        return model in KNOWN_MODELS

    def model_for(self, step: JudgeStep | None = None) -> str:
        """Resolve (and validate) the model for ``step`` via the step router."""
        return owned_model(
            self._step_models.for_step(step), owns=self._owns, provider=PROVIDER
        )

    def _resolve(self, step: JudgeStep | None, model: str | None) -> str:
        """An explicit ``model`` (validated) wins over ``step`` routing."""
        if model is not None:
            return owned_model(model, owns=self._owns, provider=PROVIDER)
        return self.model_for(step)

    @staticmethod
    def _record_usage(message: Any) -> None:
        """Record the Anthropic response's token usage on the active accumulator.

        ``getattr``-safe: a response (or a test fake) without ``usage`` records
        nothing. Anthropic reports ``input_tokens``/``output_tokens``.
        """
        usage = getattr(message, "usage", None)
        record_usage(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )

    @staticmethod
    def _thinking_kwargs(model: str) -> dict[str, Any]:
        """Per-model ``thinking`` for ``messages.parse``.

        Adaptive-thinking models get ``thinking={"type": "adaptive"}`` so they
        reason before committing to a score; models that don't support it (e.g.
        Haiku 4.5, used as the cheap bulk tier) omit ``thinking`` entirely, since
        sending adaptive thinking to them returns a 400.
        """
        if model in ADAPTIVE_THINKING_MODELS:
            return {"thinking": {"type": "adaptive"}}
        return {}

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
        message = self._client.messages.parse(
            model=model,
            max_tokens=self.max_tokens,
            **self._thinking_kwargs(model),
            system=self.system_prompt,
            messages=[{"role": "user", "content": content}],
            output_format=_ScoreResponse,
        )
        self._record_usage(message)
        parsed: _ScoreResponse = message.parsed_output
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
        model = self._resolve(step, model)
        message = self._client.messages.parse(
            model=model,
            max_tokens=self.max_tokens,
            **self._thinking_kwargs(model),
            system=self.system_prompt,
            messages=[{"role": "user", "content": render_prompt(instruction, turn)}],
            output_format=schema,
        )
        self._record_usage(message)
        return message.parsed_output

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` via the configured embeddings backend.

        Anthropic exposes no embeddings endpoint, so this delegates to the
        ``embedder`` injected at construction (e.g. an ``OpenAIJudge``), forwarding
        an explicit ``model`` for the backend to validate. Raises a Spanish error
        when no backend is configured.
        """
        if self._embedder is None:
            raise NotImplementedError(
                "Anthropic no ofrece un endpoint de embeddings. Configura un "
                "proveedor de embeddings (p. ej. OPENAI_API_KEY) para las métricas "
                "que los requieran, como la relevancia de la respuesta."
            )
        return self._embedder.embed(texts=texts, model=model)
