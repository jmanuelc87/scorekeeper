"""``Judge`` implementation backed by the Claude Agent SDK.

Every call runs through the Claude Code CLI that ``claude-agent-sdk`` bundles, so
scoring authenticates with the local Claude Code session instead of an
``ANTHROPIC_API_KEY``. That makes it the **end-to-end testing** backend: run the
whole evaluation pipeline with no API key to configure, against the same Claude
models the Anthropic judge uses (this judge shares that judge's model allow-list,
so the per-call model pins in ``metrics/catalog`` resolve unchanged).

Where there is no interactive session to borrow — in Docker, where the container
has no ``~/.claude`` credentials — a Claude Code OAuth token (``claude setup-token``)
stands in for it: pass it as ``oauth_token`` and every query hands the CLI subprocess
a ``CLAUDE_CODE_OAUTH_TOKEN``.

The judge is deliberately *not* tool-using: each call disables the built-in tools
and caps the session at one turn, so it is a plain rubric evaluation with
structured (JSON-schema) output — the same contract the other judges honor — and
never touches the filesystem or asks for a permission decision.

The SDK is async while the ``Judge`` seam is synchronous; each call therefore
drives it with ``asyncio.run``. That is safe because judge calls already run in an
``asyncio.to_thread`` worker (see ``core.runner._evaluate_metrics``), never on the
event loop.

``claude_agent_sdk`` is imported lazily — only when the judge builds its own client
— so importing this module never requires the SDK and tests can inject a fake.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics.judges.anthropic_judge import (
    ADAPTIVE_THINKING_MODELS,
    DEFAULT_MODEL,
    KNOWN_MODELS,
)
from scorekeeper.core.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    JudgeError,
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

PROVIDER = "Claude Agent"

# Wall-clock ceiling (seconds) for one CLI-backed call; see Settings.judge_timeout_seconds.
# The SDK has no per-request timeout of its own, so it is applied around the query.
DEFAULT_TIMEOUT_SECONDS = 900.0


class _AgentCallError(RuntimeError):
    """A CLI-reported failure for one query.

    Carries the failing API call's HTTP status (when the CLI reported one) as
    ``status_code``, which is exactly what ``judge_call`` duck-types on to decide
    whether the failure is provider throttling worth retrying.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentJudge(StructuredDecisions):
    """Score turns with Claude through the Agent SDK, via JSON-schema output.

    ``client`` may be injected (tests); otherwise the ``claude_agent_sdk`` module
    itself is used — the judge needs only its ``query`` and ``ClaudeAgentOptions``.
    Adaptive thinking is enabled on the models that support it so the model reasons
    before committing to a score.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        system_prompt: str | None = None,
        embedder: Any | None = None,
        step_models: StepModels | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        oauth_token: str | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.timeout = timeout
        # Environment handed to the CLI subprocess. A Claude Code OAuth token is what
        # authenticates the judge where no interactive session exists (a container has
        # no ~/.claude credentials); with none configured the mapping stays empty and
        # the CLI uses whatever the inherited environment already provides.
        self._env = {"CLAUDE_CODE_OAUTH_TOKEN": oauth_token} if oauth_token else {}
        # Route each JudgeStep to a model. With no overrides every step resolves to
        # ``model``, so the judge behaves exactly as a single-model judge.
        self._step_models = step_models or StepModels(model)
        # The Agent SDK exposes no embeddings endpoint; embedding metrics delegate to
        # this backend (any object with an ``embed()`` method, e.g. an OpenAIJudge).
        self._embedder = embedder
        if client is None:
            import claude_agent_sdk  # lazy: only needed for a real CLI-backed call

            client = claude_agent_sdk
        self._client: Any = client

    def _owns(self, model: str) -> bool:
        """Whether ``model`` is a Claude model this judge may call."""
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
    def _record_usage(result: Any) -> None:
        """Record the query result's token usage on the active accumulator.

        The SDK reports usage as a plain dict on the result message; a result (or a
        test fake) without one records nothing. The CLI prompt-caches, and it reports
        cached input separately from ``input_tokens`` — which is then a tiny number
        that would understate the call by orders of magnitude — so the cache counters
        are added back into the input side.
        """
        usage = getattr(result, "usage", None) or {}
        record_usage(
            input_tokens=(
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
            ),
            output_tokens=usage.get("output_tokens"),
        )

    @staticmethod
    def _thinking_kwargs(model: str) -> dict[str, Any]:
        """Per-model ``thinking`` option for the query.

        Mirrors the Anthropic judge: adaptive-thinking models reason before
        committing to a score, and models that don't support it (e.g. Haiku 4.5, the
        cheap bulk tier) omit ``thinking`` entirely.
        """
        if model in ADAPTIVE_THINKING_MODELS:
            return {"thinking": {"type": "adaptive"}}
        return {}

    async def _drain(self, *, prompt: str, options: Any) -> Any:
        """Run one query and return its terminal result message (``None`` if absent).

        The result message is picked out by duck-typing on ``structured_output`` —
        only that message carries it — so this module never imports the SDK's types.
        """
        result = None
        async for message in self._client.query(prompt=prompt, options=options):
            if hasattr(message, "structured_output"):
                result = message
        return result

    def _query(self, *, prompt: str, model: str, schema: type[BaseModel]) -> Any:
        """Run one CLI-backed query synchronously and return its result message."""
        options = self._client.ClaudeAgentOptions(
            model=model,
            system_prompt=self.system_prompt,
            # No tools: this judge evaluates a rubric, it does not act. The turn
            # budget leaves room for the SDK to retry a malformed structured answer.
            tools=[],
            max_turns=5,
            output_format={"type": "json_schema", "schema": schema.model_json_schema()},
            env=self._env,
            **self._thinking_kwargs(model),
        )
        return asyncio.run(
            asyncio.wait_for(self._drain(prompt=prompt, options=options), self.timeout)
        )

    def _call(self, *, prompt: str, model: str, schema: type[T], action: str) -> T:
        """Run one query with retries and return its output parsed as ``schema``.

        The CLI reports a failed query as a result message rather than by raising, so
        the error check lives *inside* the retried operation: that is what lets a 429
        or 529 reach ``judge_call``'s backoff instead of surfacing as a hard failure.
        """

        def _operation() -> Any:
            result = self._query(prompt=prompt, model=model, schema=schema)
            if result is None:
                raise _AgentCallError("la consulta no devolvió ningún resultado")
            if getattr(result, "is_error", False):
                detail = "; ".join(getattr(result, "errors", None) or []) or getattr(
                    result, "subtype", "error"
                )
                raise _AgentCallError(
                    detail, status_code=getattr(result, "api_error_status", None)
                )
            return result

        result = judge_call(_operation, provider=PROVIDER, model=model, action=action)
        # Recorded before the parse check: the tokens were spent even if the model
        # returned output that does not satisfy the schema.
        self._record_usage(result)
        parsed = require_parsed(
            getattr(result, "structured_output", None),
            provider=PROVIDER,
            model=model,
            action=action,
        )
        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            raise JudgeError(
                f"El juez de {PROVIDER} (modelo {model!r}) devolvió una salida "
                f"estructurada que no cumple el esquema durante {action}: {exc}"
            ) from exc

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
        prompt = f"{render_prompt(rubric, turn)}\n\n{spec.instruction_es}"
        with record_judge_call(
            step=step, model=model, system_prompt=self.system_prompt, prompt=prompt
        ):
            parsed = self._call(
                prompt=prompt,
                model=model,
                schema=_ScoreResponse,
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
        prompt = render_prompt(instruction, turn)
        with record_judge_call(
            step=step, model=model, system_prompt=self.system_prompt, prompt=prompt
        ):
            return self._call(
                prompt=prompt,
                model=model,
                schema=schema,
                action="la extracción",
            )

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` via the configured embeddings backend.

        The Agent SDK exposes no embeddings endpoint, so this delegates to the
        ``embedder`` injected at construction (e.g. an ``OpenAIJudge``), forwarding an
        explicit ``model`` for the backend to validate. Raises a Spanish error when no
        backend is configured.
        """
        if self._embedder is None:
            raise NotImplementedError(
                "El SDK de agentes de Claude no ofrece un endpoint de embeddings. "
                "Configura un proveedor de embeddings (OPENAI_API_KEY, con "
                "OPENAI_BASE_URL si usas un servidor local compatible con OpenAI) "
                "para las métricas que los requieran, como la relevancia de la respuesta."
            )
        return self._embedder.embed(texts=texts, model=model)
