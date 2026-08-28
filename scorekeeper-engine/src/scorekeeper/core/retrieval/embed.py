"""Embedding seam for the retrieval pipeline's embedding phase.

``OpenAIEmbedder`` builds and owns **its own** OpenAI client rather than reusing
``metrics.judges.OpenAIJudge``: embedding retrieved documents is retrieval work, not
judging work, and it must keep running whatever judge provider a run is configured with
(Anthropic offers no embedding endpoint, LM Studio's model is the wrong width). The
lazy-import shape mirrors ``core.retrieval.parser``, the pipeline's other direct SDK
consumer; the ``max_retries=0`` and explicit timeout mirror the judge's client.

Vectors are stored in a fixed-width pgvector column (``db.models.EMBEDDING_DIMENSIONS``),
so a model of another width cannot be persisted — this is checked here and raised as
:class:`EmbedError` rather than left to fail deep in an INSERT. Keep
``openai_embedding_model`` and that constant in agreement: at 768 the column takes
nomic-embed-text and rejects OpenAI's 1536-wide ``text-embedding-3-small``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

# Batch size and client timeout ceiling for one embeddings call.
DEFAULT_TIMEOUT_SECONDS = 120.0


class EmbedError(Exception):
    """Raised when texts cannot be embedded (API failure, or an unusable vector width)."""


@runtime_checkable
class Embedder(Protocol):
    """Turn texts into vectors, one per input, in the same order."""

    def embed(self, texts: "Sequence[str]") -> list[list[float]]:
        """Embed every text in ``texts``; raises :class:`EmbedError` on failure."""
        ...


class OpenAIEmbedder:
    """Embed through the OpenAI embeddings endpoint with a client of its own.

    ``client`` is the seam for tests: any object exposing ``embeddings.create``. When
    ``None`` a real ``openai.OpenAI`` is built lazily on first use, so importing this
    module needs neither the SDK (optional ``judges`` extra) nor an API key.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        dimensions: int | None = None,
        batch_size: int | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: Any = None,
    ) -> None:
        from scorekeeper.config.settings import get_settings

        settings = get_settings()
        self.model = model or settings.openai_embedding_model
        self._api_key = api_key or settings.openai_api_key
        self._base_url = base_url or settings.openai_base_url
        self._batch_size = batch_size or settings.embedding_batch_size
        self._timeout = timeout
        self._client = client
        if dimensions is None:
            from scorekeeper.db.models import EMBEDDING_DIMENSIONS

            dimensions = EMBEDDING_DIMENSIONS
        self._dimensions = dimensions

    def embed(self, texts: "Sequence[str]") -> list[list[float]]:
        """Embed ``texts`` in batches of ``embedding_batch_size``, order preserved."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(list(texts[start : start + self._batch_size])))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            response = self._get_client().embeddings.create(model=self.model, input=batch)
        except Exception as exc:  # SDK/transport/API failure
            raise EmbedError(
                f"Falló el cálculo de embeddings (modelo {self.model!r}): {exc}"
            ) from exc
        vectors = [item.embedding for item in response.data]
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise EmbedError(
                    f"El modelo {self.model!r} devuelve vectores de {len(vector)} "
                    f"dimensiones; la columna almacena {self._dimensions}"
                )
        return vectors

    def _get_client(self) -> Any:
        if self._client is None:
            import openai  # lazy: only needed when building a real client

            if not self._api_key:
                raise EmbedError("No hay openai_api_key configurada para los embeddings")
            # max_retries=0: no retry policy in this phase yet — a failed document is
            # recorded and skipped, best-effort, like every other retrieval failure.
            self._client = openai.OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=0,
                timeout=self._timeout,
            )
        return self._client
