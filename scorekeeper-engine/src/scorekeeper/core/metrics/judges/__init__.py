"""Concrete ``Judge`` implementations and a config-driven factory.

The metric taxonomy depends only on the ``Judge`` Protocol; the real judges land
here (as anticipated by ``metrics/judge.py``). Importing this package does not
require any LLM SDK — each judge imports its SDK lazily, only when it has to build
its own client. Select one at runtime with :func:`make_judge`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scorekeeper.config.settings import get_settings
from scorekeeper.core.metrics.judge import JudgeStep
from scorekeeper.core.metrics.judges.agent_judge import AgentJudge
from scorekeeper.core.metrics.judges.anthropic_judge import AnthropicJudge
from scorekeeper.core.metrics.judges.base import JudgeError, StepModels
from scorekeeper.core.metrics.judges.openai_judge import OpenAIJudge
from scorekeeper.core.metrics.judges.tracing import TracingJudge

if TYPE_CHECKING:
    from scorekeeper.config.settings import Settings
    from scorekeeper.core.metrics.judge import Judge

__all__ = [
    "AgentJudge",
    "AnthropicJudge",
    "JudgeError",
    "OpenAIJudge",
    "TracingJudge",
    "make_judge",
]


def _step_models(settings: Settings, default_model: str) -> StepModels:
    """Build the per-step model map for ``default_model`` from ``settings``.

    Unset overrides fall back to ``default_model`` (``StepModels`` ignores falsy
    values), so with none configured every step routes to the provider's default
    judge model — identical to the previous single-model behavior.
    """
    # No VERIFY override: no metric routes a model through the VERIFY step via config
    # (faithfulness pins its own), so exposing one would be a no-op. StepModels still
    # supports VERIFY as a role — this only omits the settings-driven knob for it.
    return StepModels(
        default_model,
        {
            JudgeStep.EXTRACT: settings.judge_extract_model,
            JudgeStep.SCORE: settings.judge_score_model,
        },
    )


def _embedder(settings: Settings) -> Judge | None:
    """OpenAI-backed embeddings backend for providers with no embeddings endpoint.

    Anthropic and the Agent SDK offer none, so similarity metrics borrow this one.
    ``openai_base_url`` points it at any OpenAI-compatible endpoint (e.g. a local
    server), so such a run embeds without reaching api.openai.com. Returns ``None``
    when no key is configured — ``embed()`` then raises only if a metric needs it.
    """
    if not settings.openai_api_key:
        return None
    return OpenAIJudge(
        model=settings.openai_judge_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        embedding_model=settings.openai_embedding_model,
        timeout=settings.judge_timeout_seconds,
    )


def _traced(judge: Judge, settings: Settings) -> Judge:
    """Wrap ``judge`` in a ``TracingJudge`` when ``judge_trace_enabled`` is set.

    Off by default, so the returned judge is the bare provider judge unless the
    operator opts in — tracing is observational and never changes scoring.
    """
    if settings.judge_trace_enabled:
        return TracingJudge(judge)
    return judge


def make_judge(settings: Settings | None = None) -> Judge:
    """Build the judge configured by ``settings`` (defaults to ``get_settings()``).

    Reads ``judge_provider`` and the matching model/API-key settings. Raises a
    Spanish ``ValueError`` when the provider is unknown or its API key is missing.
    When ``judge_trace_enabled`` is set, the built judge is wrapped so every LLM
    API call is traced (see :class:`~scorekeeper.core.metrics.judges.tracing.TracingJudge`).
    """
    settings = settings or get_settings()
    provider = settings.judge_provider.strip().lower()

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError(
                "Falta ANTHROPIC_API_KEY para el juez de Anthropic."
            )
        # Anthropic has no embeddings endpoint: attach an OpenAI-backed embedder
        # when a key is available so similarity metrics still work.
        embedder = _embedder(settings)
        return _traced(
            AnthropicJudge(
                model=settings.anthropic_judge_model,
                api_key=settings.anthropic_api_key,
                max_tokens=settings.judge_max_tokens,
                system_prompt=settings.judge_system_prompt,
                embedder=embedder,
                step_models=_step_models(settings, settings.anthropic_judge_model),
                timeout=settings.judge_timeout_seconds,
            ),
            settings,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("Falta OPENAI_API_KEY para el juez de OpenAI.")
        return _traced(
            OpenAIJudge(
                model=settings.openai_judge_model,
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                system_prompt=settings.judge_system_prompt,
                embedding_model=settings.openai_embedding_model,
                step_models=_step_models(settings, settings.openai_judge_model),
                timeout=settings.judge_timeout_seconds,
            ),
            settings,
        )

    if provider in ("agent", "claude-agent", "claude_agent"):
        # Claude Agent SDK judge. No API key gate: it authenticates through the local
        # Claude Code session, or — where there is none, as in Docker — through the
        # CLAUDE_CODE_OAUTH_TOKEN forwarded to the CLI. Like the Anthropic judge it has
        # no embeddings endpoint, so an OpenAI-backed embedder is attached when a key
        # is available.
        embedder = _embedder(settings)
        return _traced(
            AgentJudge(
                model=settings.agent_judge_model,
                system_prompt=settings.judge_system_prompt,
                embedder=embedder,
                step_models=_step_models(settings, settings.agent_judge_model),
                timeout=settings.judge_timeout_seconds,
                oauth_token=settings.claude_code_oauth_token,
            ),
            settings,
        )

    raise ValueError(
        f"Proveedor de juez desconocido: {settings.judge_provider!r}. "
        "Usa 'anthropic', 'openai' o 'agent'."
    )
