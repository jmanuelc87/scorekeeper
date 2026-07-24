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
from scorekeeper.metrics.judges.base import JudgeError, StepModels
from scorekeeper.metrics.judges.lmstudio_judge import LMStudioJudge
from scorekeeper.metrics.judges.openai_judge import OpenAIJudge
from scorekeeper.metrics.judges.tracing import TracingJudge

if TYPE_CHECKING:
    from scorekeeper.config import Settings
    from scorekeeper.metrics.judge import Judge

__all__ = [
    "AnthropicJudge",
    "JudgeError",
    "LMStudioJudge",
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
    API call is traced (see :class:`~scorekeeper.metrics.judges.tracing.TracingJudge`).
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
        return _traced(
            AnthropicJudge(
                model=settings.anthropic_judge_model,
                api_key=settings.anthropic_api_key,
                max_tokens=settings.judge_max_tokens,
                system_prompt=settings.judge_system_prompt,
                embedder=embedder,
                step_models=_step_models(settings, settings.anthropic_judge_model),
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
                system_prompt=settings.judge_system_prompt,
                embedding_model=settings.openai_embedding_model,
                step_models=_step_models(settings, settings.openai_judge_model),
            ),
            settings,
        )

    if provider in ("lmstudio", "local", "lm-studio"):
        # Local OpenAI-compatible server (LM Studio). No API key gate: it needs none.
        # Remaps every requested/pinned model to the loaded local model, so all
        # metrics run end-to-end (see LMStudioJudge).
        return _traced(
            LMStudioJudge(
                model=settings.lmstudio_judge_model,
                base_url=settings.lmstudio_base_url,
                api_key=settings.lmstudio_api_key,
                system_prompt=settings.judge_system_prompt,
                embedding_model=settings.lmstudio_embedding_model,
            ),
            settings,
        )

    raise ValueError(
        f"Proveedor de juez desconocido: {settings.judge_provider!r}. "
        "Usa 'anthropic', 'openai' o 'lmstudio'."
    )
