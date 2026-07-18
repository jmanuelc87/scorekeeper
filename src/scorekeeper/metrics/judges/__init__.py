"""Concrete ``Judge`` implementations and a config-driven factory.

The metric taxonomy depends only on the ``Judge`` Protocol; the real judges land
here (as anticipated by ``metrics/judge.py``). Importing this package does not
require any LLM SDK — each judge imports its SDK lazily, only when it has to build
its own client. Select one at runtime with :func:`make_judge`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scorekeeper.config import get_settings
from scorekeeper.metrics.judge import JudgeStep
from scorekeeper.metrics.judges.anthropic_judge import AnthropicJudge
from scorekeeper.metrics.judges.base import StepModels
from scorekeeper.metrics.judges.openai_judge import OpenAIJudge

if TYPE_CHECKING:
    from scorekeeper.config import Settings
    from scorekeeper.metrics.judge import Judge

__all__ = ["AnthropicJudge", "OpenAIJudge", "make_judge"]


def _step_models(settings: Settings, default_model: str) -> StepModels:
    """Build the per-step model map for ``default_model`` from ``settings``.

    Unset overrides fall back to ``default_model`` (``StepModels`` ignores falsy
    values), so with none configured every step routes to the provider's default
    judge model — identical to the previous single-model behavior.
    """
    return StepModels(
        default_model,
        {
            JudgeStep.EXTRACT: settings.judge_extract_model,
            JudgeStep.VERIFY: settings.judge_verify_model,
            JudgeStep.SCORE: settings.judge_score_model,
        },
    )


def make_judge(settings: Settings | None = None) -> Judge:
    """Build the judge configured by ``settings`` (defaults to ``get_settings()``).

    Reads ``judge_provider`` and the matching model/API-key settings. Raises a
    Spanish ``ValueError`` when the provider is unknown or its API key is missing.
    """
    settings = settings or get_settings()
    provider = settings.judge_provider.strip().lower()

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError(
                "Falta ANTHROPIC_API_KEY para el juez de Anthropic."
            )
        # Anthropic has no embeddings endpoint: attach an OpenAI-backed embedder
        # when a key is available so similarity metrics still work; otherwise leave
        # it unset (embed() will raise only if a metric actually needs it).
        embedder = None
        if settings.openai_api_key:
            embedder = OpenAIJudge(
                model=settings.openai_judge_model,
                api_key=settings.openai_api_key,
                embedding_model=settings.openai_embedding_model,
            )
        return AnthropicJudge(
            model=settings.anthropic_judge_model,
            api_key=settings.anthropic_api_key,
            max_tokens=settings.judge_max_tokens,
            system_prompt=settings.judge_system_prompt,
            embedder=embedder,
            step_models=_step_models(settings, settings.anthropic_judge_model),
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("Falta OPENAI_API_KEY para el juez de OpenAI.")
        return OpenAIJudge(
            model=settings.openai_judge_model,
            api_key=settings.openai_api_key,
            system_prompt=settings.judge_system_prompt,
            embedding_model=settings.openai_embedding_model,
            step_models=_step_models(settings, settings.openai_judge_model),
        )

    raise ValueError(
        f"Proveedor de juez desconocido: {settings.judge_provider!r}. "
        "Usa 'anthropic' u 'openai'."
    )
