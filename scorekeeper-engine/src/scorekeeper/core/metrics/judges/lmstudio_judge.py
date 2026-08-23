"""``Judge`` implementation backed by a local LM Studio server.

LM Studio exposes an OpenAI-compatible HTTP API (``/v1/chat/completions``,
``/v1/embeddings``), so this judge is a thin variant of :class:`OpenAIJudge`
pointed at the local server. It exists for **end-to-end testing**: run the whole
evaluation pipeline against a locally-hosted model with no external cost or API
key.

Two deliberate differences from ``OpenAIJudge``:

* The client's ``base_url`` targets the local server (default
  ``http://localhost:1234/v1``).
* Every requested/pinned model id is **remapped to the single loaded local
  model**. LM Studio typically serves one chat model, yet some metrics pin cloud
  model ids per call (e.g. faithfulness pins Anthropic models). Remapping lets all
  metrics run end-to-end against whatever is loaded, and the ownership validation
  is a no-op (the local judge owns everything).

The ``openai`` SDK is imported lazily — only when the judge builds its own client —
so importing this module never requires the SDK and tests can inject a fake client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scorekeeper.core.metrics.judges.openai_judge import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    OpenAIJudge,
)

if TYPE_CHECKING:
    from scorekeeper.core.metrics.judge import JudgeStep

PROVIDER = "LM Studio"
DEFAULT_BASE_URL = "http://localhost:1234/v1"


class LMStudioJudge(OpenAIJudge):
    """Score turns with a local LM Studio model via its OpenAI-compatible API.

    Inherits ``score``/``structured`` from :class:`OpenAIJudge` unchanged — they use
    ``chat.completions.parse`` with a JSON-schema ``response_format``, which LM Studio
    (0.3+) supports as structured output. Model resolution is overridden so every
    call hits the single configured local model regardless of the requested id.
    """

    # Report "LM Studio" (not the inherited "OpenAI") in the descriptive errors the
    # inherited call methods raise, so a local-server failure names the right backend.
    provider = PROVIDER

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "lm-studio",
        client: Any | None = None,
        system_prompt: str | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if client is None:
            import openai  # lazy: only needed when building a real client

            # LM Studio ignores the API key, but the SDK requires a non-empty one.
            # max_retries=0 for the same reason as the cloud judges: ``judge_call`` owns
            # retrying, so the SDK must not add a second, invisible budget on top.
            client = openai.OpenAI(
                base_url=base_url, api_key=api_key, max_retries=0, timeout=timeout
            )
        # No step_models: resolution is overridden below to always use ``model``, so
        # per-step routing would be a no-op.
        super().__init__(
            model=model,
            client=client,
            system_prompt=system_prompt,
            embedding_model=embedding_model,
        )

    def _owns(self, model: str) -> bool:
        """The local judge owns every model — validation is a no-op here."""
        return True

    def _owns_embedding(self, model: str) -> bool:
        """The local judge owns every embedding model — validation is a no-op."""
        return True

    def model_for(self, step: JudgeStep | None = None) -> str:
        """Always the single loaded local model, ignoring per-step routing."""
        return self.model

    def resolve_model(self, step: JudgeStep | None, model: str | None) -> str:
        """Remap any requested/pinned model to the one loaded local model."""
        return self.model

    def embed(self, *, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed via LM Studio, ignoring any requested model for the loaded one."""
        return super().embed(texts=texts)
