"""Tests for the embedding phase (chunk each retrieved document, then embed it)."""

from __future__ import annotations

from typing import Sequence

from scorekeeper.core.retrieval.embed import EmbedError
from scorekeeper.core.services.embedding import embed_document, embed_turn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import (
    BenchmarkRun,
    PlatformExecution,
    RetrievedContextDocument,
    RetrievedDocumentEmbedding,
    ScenarioResult,
    Turn,
    UseCase,
)


class _Embedder:
    """Returns a distinct unit vector per text; optionally fails instead."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.exc is not None:
            raise self.exc
        self.calls.append(list(texts))
        return [[float(i), 0.0] for i, _ in enumerate(texts)]


async def _document(
    session: AsyncSession,
    sentence_count: int,
    *,
    pages: list[int | None] | None = None,
) -> RetrievedContextDocument:
    """A persisted document with ``sentence_count`` sentences and no chunks yet.

    Persisted because the chunk rows reference it by id, and because the phase asks
    whether it is already chunked in SQL rather than through the relationship.
    """
    turn = await _a_turn(session)
    document = RetrievedContextDocument(
        turn_id=turn.id,
        rank=0,
        name="n",
        document="d.pdf",
        sentences=[
            {
                "page": 1 if pages is None else pages[i],
                "index": i,
                "text": f"Oración {i}.",
            }
            for i in range(sentence_count)
        ],
    )
    session.add(document)
    await session.flush()
    return document


async def _a_turn(session: AsyncSession) -> Turn:
    use_case = UseCase(name="caso")
    session.add(use_case)
    await session.flush()
    run = BenchmarkRun()
    scenario = ScenarioResult(run=run, scenario_id="esc-1", use_case=use_case)
    platform_exec = PlatformExecution(scenario_result=scenario, platform="claude")
    # ``retrieved_documents`` set explicitly: production loads the turn with a
    # ``selectinload``, and a collection never touched lazy-loads once flushed.
    turn = Turn(
        platform_execution=platform_exec,
        turn_number=1,
        prompt="p",
        response="r",
        retrieved_documents=[],
    )
    session.add(turn)
    await session.flush()
    return turn


async def _chunks(session: AsyncSession, document: RetrievedContextDocument):
    """The stored chunk rows of ``document``, in ``chunk_index`` order."""
    stmt = (
        select(RetrievedDocumentEmbedding)
        .where(RetrievedDocumentEmbedding.retrieved_document_id == document.id)
        .order_by(RetrievedDocumentEmbedding.chunk_index)
    )
    return list((await session.execute(stmt)).scalars())


async def test_chunks_are_stored_with_consecutive_indexes_and_vectors(
    session: AsyncSession,
) -> None:
    document = await _document(session, 6)
    embedder = _Embedder()

    await embed_document(session, document, embedder)

    # Defaults are 5 sentences per chunk overlapping by 1 → step 4 → two chunks.
    rows = await _chunks(session, document)
    assert [row.chunk_index for row in rows] == [0, 1]
    assert rows[0].content.startswith("Oración 0.")
    assert all(row.embedding is not None for row in rows)
    assert embedder.calls == [[row.content for row in rows]]


async def test_is_idempotent(session: AsyncSession) -> None:
    document = await _document(session, 6)
    embedder = _Embedder()
    await embed_document(session, document, embedder)
    await session.flush()

    await embed_document(session, document, embedder)
    await session.flush()

    # Second pass neither re-embeds nor duplicates rows (the unique constraint would fail).
    assert len(embedder.calls) == 1
    total = await session.scalar(
        select(func.count()).select_from(RetrievedDocumentEmbedding)
    )
    assert total == 2


async def test_a_document_without_sentences_is_skipped(session: AsyncSession) -> None:
    document = await _document(session, 0)
    embedder = _Embedder()

    await embed_document(session, document, embedder)

    assert await _chunks(session, document) == []
    assert embedder.calls == []


async def test_without_an_embedder_the_chunks_are_still_stored(
    session: AsyncSession,
) -> None:
    """The chunks are the document's only text, so they must survive a run with no key."""
    document = await _document(session, 6)

    await embed_document(session, document, None)

    rows = await _chunks(session, document)
    assert [row.chunk_index for row in rows] == [0, 1]
    assert all(row.embedding is None for row in rows)


async def test_an_embedding_failure_keeps_the_text_and_drops_only_the_vectors(
    session: AsyncSession,
) -> None:
    document = await _document(session, 6)

    await embed_document(session, document, _Embedder(exc=EmbedError("boom")))

    # Not raised: the judge still reads the document, it just cannot be narrowed.
    rows = await _chunks(session, document)
    assert len(rows) == 2
    assert all(row.embedding is None for row in rows)


async def test_embed_turn_flushes_new_documents_before_chunking(
    session: AsyncSession,
) -> None:
    """A turn retrieved in this transaction has documents with no id yet."""
    turn = await _a_turn(session)
    turn.retrieved_documents.append(
        RetrievedContextDocument(
            rank=0,
            name="n",
            document="d.pdf",
            sentences=[{"page": 1, "index": 0, "text": "Una oración."}],
            embeddings=[],
        )
    )

    await embed_turn(session, turn, _Embedder())

    assert len(await _chunks(session, turn.retrieved_documents[0])) == 1


async def test_chunks_record_where_they_came_from(session: AsyncSession) -> None:
    # 6 sentences, the last two on page 2; default windows are 5 sharing 1 → step 4.
    document = await _document(session, 6, pages=[1, 1, 1, 1, 2, 2])

    await embed_document(session, document, _Embedder())

    rows = await _chunks(session, document)
    assert [(r.sentence_start, r.sentence_end) for r in rows] == [(0, 4), (4, 5)]
    # The first window straddles the page break and is filed under its first sentence's
    # page; the second starts on page 2.
    assert [r.page for r in rows] == [1, 2]


async def test_provenance_is_stored_even_without_an_embedder(
    session: AsyncSession,
) -> None:
    document = await _document(session, 6)

    await embed_document(session, document, None)

    rows = await _chunks(session, document)
    assert all(r.embedding is None for r in rows)
    assert [(r.page, r.sentence_start, r.sentence_end) for r in rows] == [
        (1, 0, 4),
        (1, 4, 5),
    ]


async def test_documents_without_pages_store_a_null_page(session: AsyncSession) -> None:
    """A DOCX or HTML document has sentences but no page boundaries."""
    document = await _document(session, 3, pages=[None, None, None])

    await embed_document(session, document, _Embedder())

    assert all(r.page is None for r in await _chunks(session, document))
    # The sentence range is still recorded — it does not depend on pages.
    assert [(r.sentence_start, r.sentence_end) for r in await _chunks(session, document)] == [
        (0, 2)
    ]
