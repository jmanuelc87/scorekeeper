"""Tests for the chunk query — the SQL cosine ranking and its degradation.

Two halves, because the suite runs on SQLite and the ranking is PostgreSQL-only:

* the **degraded** path is executed for real here;
* the **PostgreSQL** path is checked by compiling the statement against the PostgreSQL
  dialect and asserting its shape. That is what matters about it — an ``ORDER BY`` with
  anything ahead of the distance would silently stop the HNSW index from being usable,
  and nothing in a running query would tell us.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
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
from scorekeeper.db.repositories.embeddings import (
    _add_neighbors,
    _top_k_stmt,
    chunks_for_turn,
    has_chunks,
)
from scorekeeper.core.retrieved_context import Chunk

_QUERY = [0.0, 1.0]


async def _turn_with_chunks(
    session: AsyncSession, documents: list[list[str]]
) -> tuple[Turn, list[RetrievedContextDocument]]:
    """A persisted turn with one document per inner list, chunked with its texts."""
    # Unique by name, so a test seeding two turns must reuse the existing row.
    use_case = await session.scalar(select(UseCase).where(UseCase.name == "caso"))
    if use_case is None:
        use_case = UseCase(name="caso")
        session.add(use_case)
        await session.flush()
    run = BenchmarkRun()
    scenario = ScenarioResult(run=run, scenario_id="esc-1", use_case=use_case)
    platform_exec = PlatformExecution(scenario_result=scenario, platform="claude")
    turn = Turn(
        platform_execution=platform_exec,
        turn_number=1,
        prompt="p",
        response="r",
        retrieved_documents=[],
    )
    rows: list[RetrievedContextDocument] = []
    for rank, texts in enumerate(documents):
        row = RetrievedContextDocument(
            rank=rank, name=f"n{rank}", document=f"d{rank}.pdf", embeddings=[]
        )
        row.embeddings = [
            RetrievedDocumentEmbedding(
                chunk_index=i, content=text, embedding=[1.0, 0.0]
            )
            for i, text in enumerate(texts)
        ]
        turn.retrieved_documents.append(row)
        rows.append(row)
    session.add(turn)
    await session.flush()
    return turn, rows


# -- the degraded (non-PostgreSQL) path ------------------------------------------------------


async def test_outside_postgresql_every_chunk_is_returned_in_order(
    session: AsyncSession,
) -> None:
    turn, (doc,) = await _turn_with_chunks(session, [["uno", "dos", "tres"]])

    chunks = await chunks_for_turn(
        session, turn_id=turn.id, query_embedding=_QUERY, k=1
    )

    # k=1 would narrow on PostgreSQL; here there is no ``<=>`` so nothing is dropped —
    # handing over the whole document beats handing over nothing.
    assert [(c.index, c.text) for c in chunks[doc.id]] == [
        (0, "uno"),
        (1, "dos"),
        (2, "tres"),
    ]


async def test_chunks_are_grouped_per_document(session: AsyncSession) -> None:
    turn, (a, b) = await _turn_with_chunks(session, [["a0", "a1"], ["b0"]])

    chunks = await chunks_for_turn(
        session, turn_id=turn.id, query_embedding=None, k=0
    )

    assert [c.text for c in chunks[a.id]] == ["a0", "a1"]
    assert [c.text for c in chunks[b.id]] == ["b0"]


async def test_a_turn_without_documents_yields_nothing(session: AsyncSession) -> None:
    turn, _ = await _turn_with_chunks(session, [])
    assert await chunks_for_turn(session, turn_id=turn.id, query_embedding=_QUERY, k=3) == {}


async def test_another_turns_chunks_are_not_returned(session: AsyncSession) -> None:
    _, (mine,) = await _turn_with_chunks(session, [["mío"]])
    other, _ = await _turn_with_chunks(session, [["ajeno"]])

    chunks = await chunks_for_turn(
        session, turn_id=other.id, query_embedding=None, k=0
    )

    assert mine.id not in chunks


async def test_has_chunks(session: AsyncSession) -> None:
    _, (chunked,) = await _turn_with_chunks(session, [["uno"]])
    bare = RetrievedContextDocument(
        turn_id=chunked.turn_id, rank=9, name="n", document="d.pdf", embeddings=[]
    )
    session.add(bare)
    await session.flush()

    assert await has_chunks(session, chunked.id) is True
    assert await has_chunks(session, bare.id) is False


# -- the neighbour window ---------------------------------------------------------------------
#
# ``chunks_for_turn`` only widens on the ranked (PostgreSQL) path — outside it every chunk is
# already returned — but the widening itself is ordinary ``chunk_index`` SQL, so it runs here.


async def test_a_hit_is_widened_to_the_chunks_either_side(session: AsyncSession) -> None:
    _, (doc,) = await _turn_with_chunks(session, [["c0", "c1", "c2", "c3", "c4"]])
    chunks = {doc.id: [Chunk(index=2, text="c2")]}

    await _add_neighbors(session, chunks, 1)

    assert [(c.index, c.text) for c in chunks[doc.id]] == [(1, "c1"), (2, "c2"), (3, "c3")]


async def test_the_window_stops_at_the_documents_edges(session: AsyncSession) -> None:
    """An index past either end matches no row, so no bounds are needed."""
    _, (doc,) = await _turn_with_chunks(session, [["c0", "c1", "c2"]])
    chunks = {doc.id: [Chunk(index=0, text="c0")]}

    await _add_neighbors(session, chunks, 2)

    assert [c.index for c in chunks[doc.id]] == [0, 1, 2]


async def test_adjacent_hits_share_their_window_without_duplicating(
    session: AsyncSession,
) -> None:
    _, (doc,) = await _turn_with_chunks(session, [["c0", "c1", "c2", "c3"]])
    chunks = {doc.id: [Chunk(index=1, text="c1"), Chunk(index=2, text="c2")]}

    await _add_neighbors(session, chunks, 1)

    assert [c.index for c in chunks[doc.id]] == [0, 1, 2, 3]


async def test_the_window_never_crosses_into_another_document(
    session: AsyncSession,
) -> None:
    _, (a, b) = await _turn_with_chunks(session, [["a0", "a1"], ["b0", "b1"]])
    chunks = {a.id: [Chunk(index=0, text="a0")]}

    await _add_neighbors(session, chunks, 1)

    assert [c.text for c in chunks[a.id]] == ["a0", "a1"]
    assert b.id not in chunks


async def test_neighbours_are_ignored_on_the_degraded_path(session: AsyncSession) -> None:
    """Outside PostgreSQL every chunk is returned already; widening would be a no-op."""
    turn, (doc,) = await _turn_with_chunks(session, [["uno", "dos", "tres"]])

    chunks = await chunks_for_turn(
        session, turn_id=turn.id, query_embedding=_QUERY, k=1, neighbors=1
    )

    assert [c.index for c in chunks[doc.id]] == [0, 1, 2]


# -- the PostgreSQL statement ----------------------------------------------------------------


def _compiled() -> str:
    stmt = _top_k_stmt(turn_id=uuid.uuid4(), query_embedding=_QUERY, k=3)
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_statement_is_a_lateral_top_k_per_document() -> None:
    sql = _compiled()
    assert "JOIN LATERAL" in sql
    assert "LIMIT" in sql
    # Correlated to the outer document: that is what makes it per-document rather than a
    # single global top-k, which would drop whole documents and break contextual_precision.
    assert "retrieved_document_embeddings.retrieved_document_id = retrieved_documents.id" in sql


def test_the_inner_order_by_is_the_bare_cosine_distance() -> None:
    """Anything ahead of ``<=>`` in the ORDER BY makes the HNSW index unusable."""
    sql = _compiled()
    inner = re.search(r"ORDER BY (.+?)\s*\n?\s*LIMIT", sql, re.DOTALL)
    assert inner is not None
    order_by = inner.group(1).strip()
    assert "<=>" in order_by
    # One term only — no ``(embedding IS NULL)`` NULLS-LAST key ahead of the distance
    # (NULL distances already sort last) and no tie-breaker after it.
    assert order_by.count(",") == 0


def test_the_embedding_column_is_never_selected() -> None:
    """The vectors staying in the database is half the point of ranking in SQL."""
    sql = _compiled()
    projections = sql[: sql.index("FROM")]
    assert "embedding" not in projections


def test_provenance_columns_are_not_read_back() -> None:
    """Page and sentence range are stored for citation, not for the judge.

    Selecting them would put them in the ``Chunk`` value object, which
    ``metrics.fingerprint`` hashes whole — every stored ``scoring_key`` would invalidate
    and the whole corpus would re-score.
    """
    sql = _compiled()
    for column in ("page", "sentence_start", "sentence_end"):
        assert column not in sql
