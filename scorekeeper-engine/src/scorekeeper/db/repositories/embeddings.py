"""Queries over ``retrieved_document_embeddings`` — the chunks a judge reads.

A retrieved document's text lives only on its chunks, and a judge is handed just the few
closest to the turn's prompt. That ranking runs **in PostgreSQL**, through pgvector's
``<=>`` cosine-distance operator, rather than in Python: the chunks stay in the database
and their 1536-float vectors never cross into the process.

**Shape matters here.** The per-document subquery is written as a bare
``ORDER BY embedding <=> :q LIMIT :k`` because that is the only form the
``ix_retrieved_document_embeddings_hnsw`` index (HNSW, ``vector_cosine_ops``) can serve.
Two consequences worth stating plainly:

* Chunks with no embedding sort **last for free** — ``<=>`` on NULL yields NULL, and NULLs
  sort last in an ascending order. Spelling that out as ``ORDER BY (embedding IS NULL), …``
  would give the same rows and *disable the index*, so it is deliberately not written.
* Whether the planner actually picks HNSW is its call, not ours. Filtered by one
  ``retrieved_document_id`` the candidate set is tiny, and an exact scan over the
  foreign-key index is both faster and — unlike HNSW — exact. If it does choose HNSW,
  unvectorised chunks disappear rather than sorting last, because HNSW does not index
  NULLs. Verify with ``EXPLAIN`` before claiming either.

Outside PostgreSQL there is no ``<=>`` (``EmbeddingColumn`` degrades to JSON on the SQLite
fallback), so ranking is skipped and every chunk is returned in document order — the same
degradation ``db.connection.run_lock`` applies to advisory locks.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.retrieved_context import Chunk
from scorekeeper.db.models import RetrievedContextDocument, RetrievedDocumentEmbedding


def _ranks_in_sql(session: AsyncSession) -> bool:
    """Whether this session can run pgvector's distance operators."""
    bind = session.bind
    return bind is not None and bind.dialect.name == "postgresql"


async def chunks_for_turn(
    session: AsyncSession,
    *,
    turn_id: uuid.UUID,
    query_embedding: list[float] | None,
    k: int,
) -> dict[uuid.UUID, list[Chunk]]:
    """The chunks a judge should read for ``turn_id``, keyed by retrieved document.

    With a ``query_embedding`` on PostgreSQL, each document contributes its ``k`` chunks
    closest to it by cosine. Otherwise every chunk is returned: no embedder configured, a
    failed embedding phase, or a non-PostgreSQL database all mean the narrowing cannot be
    computed, and handing over the whole document is better than handing over nothing.

    Chunks always come back in ``chunk_index`` order — relevance decides *which* are kept,
    the document decides how they read, so a judge never sees one shuffled by similarity.
    The ``embedding`` column is never selected; only the text is needed here.
    """
    if query_embedding is None or k < 1 or not _ranks_in_sql(session):
        stmt = (
            select(
                RetrievedDocumentEmbedding.retrieved_document_id,
                RetrievedDocumentEmbedding.chunk_index,
                RetrievedDocumentEmbedding.content,
            )
            .join(
                RetrievedContextDocument,
                RetrievedContextDocument.id
                == RetrievedDocumentEmbedding.retrieved_document_id,
            )
            .where(RetrievedContextDocument.turn_id == turn_id)
            .order_by(RetrievedDocumentEmbedding.chunk_index)
        )
    else:
        stmt = _top_k_stmt(turn_id=turn_id, query_embedding=query_embedding, k=k)

    chunks: dict[uuid.UUID, list[Chunk]] = {}
    for document_id, index, content in await session.execute(stmt):
        chunks.setdefault(document_id, []).append(Chunk(index=index, text=content))
    return chunks


def _top_k_stmt(*, turn_id: uuid.UUID, query_embedding: list[float], k: int):
    """The PostgreSQL statement: one lateral top-``k`` per document of the turn.

    Lateral rather than a window function so the ``LIMIT`` sits inside the per-document
    subquery — a ``row_number() <= k`` filter would have to rank every chunk first, which
    is exactly the work the index exists to avoid.
    """
    closest = (
        select(
            RetrievedDocumentEmbedding.chunk_index,
            RetrievedDocumentEmbedding.content,
        )
        .where(
            RetrievedDocumentEmbedding.retrieved_document_id
            == RetrievedContextDocument.id
        )
        # The whole ORDER BY, and nothing before it: see the module docstring.
        .order_by(RetrievedDocumentEmbedding.embedding.cosine_distance(query_embedding))
        .limit(k)
        .lateral()
    )
    return (
        select(RetrievedContextDocument.id, closest.c.chunk_index, closest.c.content)
        .select_from(RetrievedContextDocument)
        .join(closest, true())
        .where(RetrievedContextDocument.turn_id == turn_id)
        .order_by(closest.c.chunk_index)
    )


async def has_chunks(session: AsyncSession, document_id: uuid.UUID) -> bool:
    """Whether ``document_id`` was already chunked.

    Asked by SQL rather than through ``RetrievedContextDocument.embeddings``: the run-tree
    loaders deliberately no longer eager-load that relationship — nothing reads chunks
    through it any more — so touching it would be a ``MissingGreenlet``, not a slow query.
    """
    return bool(
        await session.scalar(
            select(
                exists().where(
                    RetrievedDocumentEmbedding.retrieved_document_id == document_id
                )
            )
        )
    )
