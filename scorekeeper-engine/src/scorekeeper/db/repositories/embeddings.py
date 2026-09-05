"""Queries over ``retrieved_document_embeddings`` — the chunks a judge reads.

A retrieved document's text lives only on its chunks, and a judge is handed just the few
closest to the turn's response, plus the chunks immediately around them. That ranking runs
**in PostgreSQL**, through pgvector's ``<=>`` cosine-distance operator, rather than in
Python: the chunks stay in the database and their vectors never cross into the process.

**Ranking and reading are not the same set.** Similarity picks ``k`` chunks; each is then
widened into ``[n-p … n … n+p]`` by a second, ordinary statement over ``chunk_index`` (see
``_add_neighbors``), because a claim rarely begins exactly where a chunk does. The
neighbours are fetched *because they adjoin a hit*, never re-ranked, so nothing about the
vector index below applies to them.

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

from sqlalchemy import and_, exists, or_, select, true
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
    neighbors: int = 0,
) -> dict[uuid.UUID, list[Chunk]]:
    """The chunks a judge should read for ``turn_id``, keyed by retrieved document.

    With a ``query_embedding`` on PostgreSQL, each document contributes its ``k`` chunks
    closest to it by cosine. Otherwise every chunk is returned: no embedder configured, a
    failed embedding phase, or a non-PostgreSQL database all mean the narrowing cannot be
    computed, and handing over the whole document is better than handing over nothing.

    ``neighbors`` (``p``) widens each hit into the window ``[n-p … n … n+p]``: the chunks
    around a hit are fetched too, so a claim that begins in the chunk *before* the one
    similarity found is still readable. Ranking picks ``k`` chunks; the window is what a
    judge actually reads, so a document can return up to ``k * (2p + 1)`` chunks — fewer
    where windows overlap or run off either end of the document. Neighbours are fetched by
    ``chunk_index``, never re-ranked: they are included *because* they adjoin a hit.
    Ignored when there is nothing to rank, since every chunk is already returned.

    Chunks always come back in ``chunk_index`` order — relevance decides *which* are kept,
    the document decides how they read, so a judge never sees one shuffled by similarity.
    The ``embedding`` column is never selected; only the text is needed here.
    """
    ranked = query_embedding is not None and k >= 1 and _ranks_in_sql(session)
    if not ranked:
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
        assert query_embedding is not None  # implied by ``ranked``; narrows the type
        stmt = _top_k_stmt(turn_id=turn_id, query_embedding=query_embedding, k=k)

    chunks: dict[uuid.UUID, list[Chunk]] = {}
    for document_id, index, content in await session.execute(stmt):
        chunks.setdefault(document_id, []).append(Chunk(index=index, text=content))
    if ranked and neighbors > 0:
        await _add_neighbors(session, chunks, neighbors)
    return chunks


async def _add_neighbors(
    session: AsyncSession, chunks: dict[uuid.UUID, list[Chunk]], neighbors: int
) -> None:
    """Widen every hit in ``chunks`` to ``[n-p … n … n+p]``, in place and in index order.

    One extra statement, asking only for the indices the ranking did *not* already return:
    a hit adjacent to another hit costs nothing, and an index past either end of the
    document simply matches no row, so no bounds are needed. Fetching by ``chunk_index``
    keeps this off the vector index entirely.
    """
    missing = {
        document_id: {
            index
            for chunk in document_chunks
            for index in range(chunk.index - neighbors, chunk.index + neighbors + 1)
            if index >= 0
        }
        - {chunk.index for chunk in document_chunks}
        for document_id, document_chunks in chunks.items()
    }
    clauses = [
        and_(
            RetrievedDocumentEmbedding.retrieved_document_id == document_id,
            RetrievedDocumentEmbedding.chunk_index.in_(sorted(indices)),
        )
        for document_id, indices in missing.items()
        if indices
    ]
    if not clauses:
        return
    stmt = select(
        RetrievedDocumentEmbedding.retrieved_document_id,
        RetrievedDocumentEmbedding.chunk_index,
        RetrievedDocumentEmbedding.content,
    ).where(or_(*clauses))
    for document_id, index, content in await session.execute(stmt):
        chunks[document_id].append(Chunk(index=index, text=content))
    for document_chunks in chunks.values():
        document_chunks.sort(key=lambda chunk: chunk.index)


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
