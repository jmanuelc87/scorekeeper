"""The embedding phase — chunk each retrieved document and embed the chunks.

Sits between retrieval and scoring. Retrieval leaves a document's text on
``RetrievedContextDocument.sentences``; this phase groups those sentences into
overlapping windows (``core.retrieval.chunk.chunk_sentences``), embeds each window, and
stores one ``RetrievedDocumentEmbedding`` row per chunk. Scoring then reads only the
chunks closest to the turn's response, so a judge never sees a whole document.

**Best-effort, like retrieval**: an embedding failure is logged and leaves the chunks
stored *without* vectors rather than failing the turn — a judge then reads the whole
document, losing only the narrowing. It is also **idempotent**: a document that already
has chunks is skipped, so a retried turn neither re-downloads nor re-embeds.

The embedder is ``core.retrieval.embed.OpenAIEmbedder``, which owns its own OpenAI
client. It is deliberately not the judge's ``Judge.embed``: embedding retrieved documents
is retrieval work and has to keep working whatever judge provider a run uses.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.chunk import chunk_sentences
from scorekeeper.core.retrieval.embed import Embedder, EmbedError, OpenAIEmbedder
from scorekeeper.core.retrieved_context import Sentence
from scorekeeper.db.models import (
    RetrievedContextDocument,
    RetrievedDocumentEmbedding,
    Turn,
)
from scorekeeper.db.repositories import embeddings as embeddings_repo
from scorekeeper.db.repositories import runs as run_repo

logger = logging.getLogger(__name__)


def default_embedder() -> Embedder | None:
    """The configured embedder, or ``None`` when embeddings cannot run.

    Without ``openai_api_key`` there is nothing to embed with; the phase is skipped and
    scoring falls back to handing the judge every chunk a document has.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    return OpenAIEmbedder()


async def embed_turn(
    session: AsyncSession, turn: Turn, embedder: Embedder | None
) -> None:
    """Chunk one turn's retrieved documents, embedding them when ``embedder`` allows.

    ``embedder`` is required rather than defaulted: ``None`` is a meaningful value here —
    "chunk, but store no vectors" — so a caller must say which it means. Resolve the
    configured one with :func:`default_embedder`.
    """
    # The chunk rows reference their document by id, so the documents must be flushed
    # before any of them is written.
    await session.flush()
    for document in turn.retrieved_documents:
        await embed_document(session, document, embedder, turn_number=turn.turn_number)


async def embed_document(
    session: AsyncSession,
    document: RetrievedContextDocument,
    embedder: Embedder | None,
    *,
    turn_number: int | None = None,
) -> None:
    """Store one document's chunks, with their vectors when an embedder is available.

    Chunking and embedding are deliberately separable. The chunks are the document's
    *only* stored text, so they are written whatever happens; the vectors are the
    enrichment that lets scoring narrow them to the turn's response. A run without
    ``openai_api_key`` — or one whose embedding call failed — therefore still hands a
    judge the whole document rather than nothing at all.

    The rows are added to the session directly rather than through
    ``document.embeddings``: that relationship is no longer eager-loaded anywhere, so
    assigning it would trigger a lazy load — a ``MissingGreenlet`` under asyncio.
    """
    # Asked in SQL for the same reason, and it is what makes a retry cheap.
    if await embeddings_repo.has_chunks(session, document.id):
        return
    settings = get_settings()
    sentences = [Sentence.model_validate(s) for s in document.sentences or []]
    windows = chunk_sentences(
        sentences,
        size=settings.embedding_chunk_sentences,
        overlap=settings.embedding_chunk_overlap,
    )
    if not windows:
        return
    texts = [window.text for window in windows]
    vectors: list[list[float] | None] = [None] * len(texts)
    if embedder is not None:
        try:
            # The SDK call is blocking; keep it off the event loop.
            vectors = list(await asyncio.to_thread(embedder.embed, texts))
        except EmbedError:
            logger.warning(
                "No se pudieron calcular los embeddings de %s (turno %s); "
                "los chunks se guardan sin vector",
                document.document,
                turn_number,
                exc_info=True,
            )
    session.add_all(
        [
            RetrievedDocumentEmbedding(
                retrieved_document_id=document.id,
                chunk_index=index,
                content=window.text,
                embedding=vector,
                # Provenance, stored but never read back by the retrieval query: it is
                # there to cite and audit a chunk, not to feed a judge.
                page=window.page,
                sentence_start=window.sentence_start,
                sentence_end=window.sentence_end,
            )
            for index, (window, vector) in enumerate(zip(windows, vectors, strict=True))
        ]
    )
    logger.info(
        "Turno %s: %d chunk(s) de %s (%s)",
        turn_number,
        len(windows),
        document.document,
        "con embedding" if vectors[0] is not None else "sin embedding",
    )


async def embed_run(run_id: str, *, session: AsyncSession, embedder: Embedder | None = None) -> None:
    """Embed every retrieved document of a run — the synchronous path's phase.

    The Celery path embeds one turn at a time inside the chain
    (``core.services.chain._work_turn``); this walks the whole tree instead, for the
    in-process ``ingest -> retrieve -> embed -> score`` sequence.
    """
    if embedder is None:
        embedder = default_embedder()
    run = await run_repo.get_run_tree(session, run_id, metric_scores=False)
    if run is None:
        return
    for scenario in run.scenario_results:
        for platform_exec in scenario.platform_executions:
            for turn in platform_exec.turns:
                if turn.is_selected:
                    await embed_turn(session, turn, embedder)
        await session.commit()
