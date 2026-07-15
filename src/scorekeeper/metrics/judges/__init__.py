"""Concrete ``Judge`` implementations and a config-driven factory.

The metric taxonomy depends only on the ``Judge`` Protocol; the real judges land
here (as anticipated by ``metrics/judge.py``). Importing this package does not
require any LLM SDK — each judge imports its SDK lazily, only when it has to build
its own client. Select one at runtime with :func:`make_judge`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scorekeeper.config import get_settings
from scorekeeper.metrics.judges.anthropic_judge import AnthropicJudge
from scorekeeper.metrics.judges.openai_judge import OpenAIJudge

if TYPE_CHECKING:
    from scorekeeper.config import Settings
    from scorekeeper.metrics.judge import Judge

__all__ = ["AnthropicJudge", "OpenAIJudge", "make_judge"]


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
        return AnthropicJudge(
            model=settings.anthropic_judge_model,
            api_key=settings.anthropic_api_key,
            max_tokens=settings.judge_max_tokens,
            system_prompt=settings.judge_system_prompt,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("Falta OPENAI_API_KEY para el juez de OpenAI.")
        return OpenAIJudge(
            model=settings.openai_judge_model,
            api_key=settings.openai_api_key,
            system_prompt=settings.judge_system_prompt,
        )

    raise ValueError(
        f"Proveedor de juez desconocido: {settings.judge_provider!r}. "
        "Usa 'anthropic' u 'openai'."
    )
