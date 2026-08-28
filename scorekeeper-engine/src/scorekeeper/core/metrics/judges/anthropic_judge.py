"""``Judge`` implementation backed by Anthropic's Claude models.

Satisfies the ``Judge`` Protocol structurally (no inheritance), exactly like the
test ``StubJudge``. The ``anthropic`` SDK is imported lazily — only when the judge
has to build its own client — so importing this module never requires the SDK and
tests can inject a fake client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    StepModels,
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

PROVIDER = "Anthropic"

# Per-request timeout (seconds) for the provider client; see Settings.judge_timeout_seconds.
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MODEL = "claude-opus-4-8"
# Models this judge is allowed to call. An exact-match allow-list (the provider's
# model space is small and Anthropic-owned); extend it as new Claude models ship.
KNOWN_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    }
)
# Models that support adaptive thinking (a 4.6+ feature). Others — e.g.
# Haiku 4.5 — reject ``thinking={"type": "adaptive"}`` with a 400, so those calls
# omit the ``thinking`` parameter entirely (no thinking). Keep in sync as new
# adaptive-capable models are added to KNOWN_MODELS.
#
# Every Claude 5 model belongs here, and for two of them it is not optional: on
# Opus 5 thinking is on by default, and on Fable 5 it is always on — ``disabled``
# and ``budget_tokens`` are both rejected with a 400 — so ``adaptive`` is the only
# configuration either accepts.
ADAPTIVE_THINKING_MODELS = frozenset(
    {"claude-fable-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"}
)
# Models that accept the server-side refusal fallback. A safety classifier may decline a
# call (HTTP 200, ``stop_reason="refusal"``); with ``fallbacks`` the API re-runs the same
# request on the fallback model inside the same call, so a declined turn is rescued
# instead of surfacing as a ``JudgeError``. Gated like ADAPTIVE_THINKING_MODELS and for
# the same reason: sending the parameter to a model that does not support it is a 400,
# which would break *every* call rather than degrade one.
REFUSAL_FALLBACK_MODELS = frozenset({"claude-fable-5", "claude-opus-5"})
# The beta the array form of ``fallbacks`` requires. The scalar ``fallbacks="default"``
# form pairs with ``server-side-fallback-2026-07-01`` instead and returns a 400 with this
# header — and the installed SDK types accept only the array form anyway.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-06-01"


class AnthropicJudge:
    """Score turns with Claude via structured (JSON-schema) output.

    ``client`` may be injected (tests); otherwise a real ``anthropic.Anthropic``
    is built from ``api_key`` on first construction. Adaptive thinking is enabled
    so the model reasons before committing to a score.

    ``fallback_model`` arms the server-side refusal fallback for the models that
    support it (see :data:`REFUSAL_FALLBACK_MODELS`); ``None`` leaves the request
    exactly as it was before the feature existed.
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
        fallback_model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        # Route each JudgeStep to a model. With no overrides every step resolves to
        # ``model``, so the judge behaves exactly as a single-model judge.
        self._step_models = step_models or StepModels(model)
        # Validated here rather than per call: a typo in configuration should fail when
        # the judge is built, not on the first refusal months later.
        self.fallback_model = (
            owned_model(fallback_model, owns=self._owns, provider=PROVIDER)
            if fallback_model
            else None
        )
        # Anthropic offers no embeddings endpoint; embedding metrics delegate to
        # this backend (any object with an ``embed()`` method, e.g. an OpenAIJudge).
        self._embedder = embedder
        if client is None:
            import anthropic  # lazy: only needed when building a real client

            # max_retries=0: retrying is owned by ``judge_call``, which backs off with
            # jitter and honors Retry-After. Leaving the SDK's own retries on would
            # multiply the two budgets and make the total wait impossible to reason about.
            client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout)
        self._client: Any = client

    def _owns(self, model: str) -> bool:
        """Whether ``model`` is an Anthropic model this judge may call."""
        return model in KNOWN_MODELS

    def model_for(self, step: JudgeStep | None = None) -> str:
        """Resolve (and validate) the model for ``step`` via the step router."""
        return owned_model(
            self._step_models.for_step(step), owns=self._owns, provider=PROVIDER
        )

    def resolve_model(self, step: JudgeStep | None, model: str | None) -> str:
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

    def _fallback_kwargs(self, model: str) -> dict[str, Any]:
        """Per-model ``fallbacks``/``betas`` for the refusal rescue.

        Empty unless a fallback model is configured *and* the calling model supports
        the feature *and* the two differ — falling back to the model that just declined
        would only buy a second refusal.
        """
        if (
            self.fallback_model is None
            or model not in REFUSAL_FALLBACK_MODELS
            or self.fallback_model == model
        ):
            return {}
        return {
            "betas": [REFUSAL_FALLBACK_BETA],
            "fallbacks": [{"model": self.fallback_model}],
        }

    def _parse(self, **kwargs: Any) -> Any:
        """Call ``messages.parse``, on the beta namespace only when it has to be.

        ``fallbacks`` lives on ``client.beta.messages``. Routing every call through the
        beta namespace would change the request shape for models that never use the
        parameter, so the plain namespace stays the default and nothing about the
        existing path moves.
        """
        if "fallbacks" in kwargs:
            return self._client.beta.messages.parse(**kwargs)
        return self._client.messages.parse(**kwargs)

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
        with record_judge_call(
            step=step, model=model, system_prompt=self.system_prompt, prompt=content
        ):
            message = judge_call(
                lambda: self._parse(
                    model=model,
                    max_tokens=self.max_tokens,
                    **self._thinking_kwargs(model),
                    **self._fallback_kwargs(model),
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": content}],
                    output_format=_ScoreResponse,
                ),
                provider=PROVIDER,
                model=model,
                action="la puntuación",
            )
            # Recorded inside the block so the tokens land on this call's record too.
            self._record_usage(message)
        # Usage is recorded before the parse check: the tokens were spent even if the
        # model refused or returned output that does not satisfy the schema.
        parsed: _ScoreResponse = require_parsed(
            message.parsed_output,
            provider=PROVIDER,
            model=model,
            action="la puntuación",
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
        with record_judge_call(
            step=step, model=model, system_prompt=self.system_prompt, prompt=content
        ):
            message = judge_call(
                lambda: self._parse(
                    model=model,
                    max_tokens=self.max_tokens,
                    **self._thinking_kwargs(model),
                    **self._fallback_kwargs(model),
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": content}],
                    output_format=schema,
                ),
                provider=PROVIDER,
                model=model,
                action="la extracción",
            )
            self._record_usage(message)
        return require_parsed(
            message.parsed_output,
            provider=PROVIDER,
            model=model,
            action="la extracción",
        )

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
