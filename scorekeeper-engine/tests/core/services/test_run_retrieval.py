"""Tests for the retrieval+scoring orchestration: retrieve_run and the Celery wiring."""

from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.db.models import BenchmarkRun, Turn
from scorekeeper.core.services import runs as run_service
from scorekeeper.core.services.ingestion import UploadedFile, ingest_evaluation
from scorekeeper.core.services.retrieval import retrieve_run
from scorekeeper.core.retrieval.types import (
    STATUS_EN_RECUPERACION,
    DocType,
    DocumentLocator,
    ExtractedContent,
    RetrievalOutcome,
    RetrievalReport,
    SourceFormat,
    SourceRef,
)
from scorekeeper.core.runner import MAX_TURN_ATTEMPTS, STATUS_FALLIDO


class _FakePipeline:
    """A RetrievalPipeline stub: each cell yields one RETRIEVED doc echoing the cell text."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self._raise = raise_exc
        self.calls: list[str] = []
        # Cells retrieved at the time of each purge_cache() call — lets a test assert both
        # how many times the cache was released and that it happened after the turns ran.
        self.purges: list[int] = []

    async def purge_cache(self) -> int:
        self.purges.append(len(self.calls))
        return len(self.calls)

    async def run(self, cell: str) -> RetrievalReport:
        self.calls.append(cell)
        if self._raise is not None:
            raise self._raise
        source = SourceRef(name="ref", url="https://h/x.html", rank=0)
        locator = DocumentLocator(
            document_url="https://h/x.html", filename="x.html", doc_type=DocType.HTML, host="h"
        )
        outcome = RetrievalOutcome.assembled(
            source, locator, ExtractedContent(text=f"md::{cell}")
        )
        return RetrievalReport(source_format=SourceFormat.PLAINTEXT, outcomes=[outcome])


def _xlsx(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _ingest_one_turn_with_context(session: AsyncSession, cell: str) -> str:
    content = _xlsx(
        ["turn", "role", "content", "retrieved_context"],
        [[1, "user", "pregunta", cell], [1, "model", "respuesta", ""]],
    )
    files = [UploadedFile(filename="esc.xlsx", content=content, scenario_id="esc")]
    run_id = await ingest_evaluation("claude", files, session=session)
    # Retrieval only runs for selected turns; select the turn so these tests exercise it.
    turn_ids = [str(t.id) for t in (await session.execute(select(Turn))).scalars().all()]
    await run_service.set_turn_selection(run_id, turn_ids, True, session=session)
    return run_id


# -- retrieve_run --------------------------------------------------------------------------


async def test_retrieve_run_populates_documents_and_sets_status(session: AsyncSession) -> None:
    run_id = await _ingest_one_turn_with_context(session, "manual (https://h/x.html)")
    pipeline = _FakePipeline()

    await retrieve_run(run_id, session=session, pipeline=pipeline)

    turn = (await session.execute(select(Turn))).scalars().one()
    assert [d.content for d in turn.retrieved_documents] == ["md::manual (https://h/x.html)"]
    # Retrieval leaves the run in the retrieval phase; scoring advances it afterwards.
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == STATUS_EN_RECUPERACION
    assert pipeline.calls == ["manual (https://h/x.html)"]


async def test_retrieve_run_skips_turns_without_source(session: AsyncSession) -> None:
    # No retrieved_context column at all → no source, pipeline never runs.
    content = _xlsx(["turn", "role", "content"], [[1, "user", "hola"], [1, "model", "hey"]])
    files = [UploadedFile(filename="esc.xlsx", content=content, scenario_id="esc")]
    run_id = await ingest_evaluation("claude", files, session=session)
    pipeline = _FakePipeline()

    await retrieve_run(run_id, session=session, pipeline=pipeline)

    turn = (await session.execute(select(Turn))).scalars().one()
    assert turn.retrieved_documents == []
    assert pipeline.calls == []


async def test_retrieve_run_skips_unselected_turns(session: AsyncSession) -> None:
    # A turn with a source but not selected for scoring must be skipped by retrieval
    # (no wasted pipeline call, no documents populated).
    content = _xlsx(
        ["turn", "role", "content", "retrieved_context"],
        [[1, "user", "pregunta", "manual (https://h/x.html)"], [1, "model", "respuesta", ""]],
    )
    files = [UploadedFile(filename="esc.xlsx", content=content, scenario_id="esc")]
    run_id = await ingest_evaluation("claude", files, session=session)  # left unselected
    pipeline = _FakePipeline()

    await retrieve_run(run_id, session=session, pipeline=pipeline)

    turn = (await session.execute(select(Turn))).scalars().one()
    assert turn.retrieved_documents == []
    assert pipeline.calls == []


async def test_retrieve_run_hard_failure_marks_fallido(session: AsyncSession) -> None:
    run_id = await _ingest_one_turn_with_context(session, "algo")
    pipeline = _FakePipeline(raise_exc=RuntimeError("kaboom"))

    with pytest.raises(RuntimeError):
        await retrieve_run(run_id, session=session, pipeline=pipeline)

    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == STATUS_FALLIDO


async def test_retrieve_run_skips_turns_already_retrieved(session: AsyncSession) -> None:
    # Resume-forward: a turn that already carries documents is left alone, so a
    # re-delivery does not re-fetch the whole run.
    run_id = await _ingest_one_turn_with_context(session, "algo")
    await retrieve_run(run_id, session=session, pipeline=_FakePipeline())

    second = _FakePipeline()
    await retrieve_run(run_id, session=session, pipeline=second)

    assert second.calls == []  # the pipeline was never asked to resolve anything
    turn = (await session.execute(select(Turn))).scalars().one()
    assert len(turn.retrieved_documents) == 1
    assert turn.attempts == 1  # and the skipped visit burned no attempt


async def test_retrieve_run_counts_an_attempt_per_turn(session: AsyncSession) -> None:
    run_id = await _ingest_one_turn_with_context(session, "algo")

    await retrieve_run(run_id, session=session, pipeline=_FakePipeline())

    turn = (await session.execute(select(Turn))).scalars().one()
    assert turn.attempts == 1


async def test_retrieve_run_abandons_a_turn_at_the_attempt_cap(session: AsyncSession) -> None:
    # A turn that keeps killing its worker during retrieval is dropped, not retried forever.
    run_id = await _ingest_one_turn_with_context(session, "algo")
    turn = (await session.execute(select(Turn))).scalars().one()
    turn.attempts = MAX_TURN_ATTEMPTS
    await session.commit()
    pipeline = _FakePipeline()

    await retrieve_run(run_id, session=session, pipeline=pipeline)

    assert pipeline.calls == []
    assert turn.retrieved_documents == []
    assert turn.attempts == MAX_TURN_ATTEMPTS  # not bumped past the cap


# -- cache cleanup -------------------------------------------------------------------------


async def test_retrieve_run_purges_cache_once_per_platform_execution(session: AsyncSession) -> None:
    # Two scenarios under one platform execution: the cache is released when the execution
    # finishes, not after every scenario, and only once every turn has been retrieved.
    files = [
        UploadedFile(
            filename=f"esc{i}.xlsx",
            content=_xlsx(
                ["turn", "role", "content", "retrieved_context"],
                [[1, "user", "pregunta", f"ref-{i}"], [1, "model", "respuesta", ""]],
            ),
            scenario_id=f"esc{i}",
        )
        for i in (1, 2)
    ]
    run_id = await ingest_evaluation("claude", files, session=session)
    # Retrieval only runs for selected turns; select them so both scenarios retrieve.
    turn_ids = [str(t.id) for t in (await session.execute(select(Turn))).scalars().all()]
    await run_service.set_turn_selection(run_id, turn_ids, True, session=session)
    pipeline = _FakePipeline()

    await retrieve_run(run_id, session=session, pipeline=pipeline)

    assert pipeline.calls == ["ref-1", "ref-2"]
    assert pipeline.purges == [2]  # one purge, after both scenarios ran


async def test_retrieve_run_purges_cache_on_failure(session: AsyncSession) -> None:
    run_id = await _ingest_one_turn_with_context(session, "algo")
    pipeline = _FakePipeline(raise_exc=RuntimeError("kaboom"))

    with pytest.raises(RuntimeError):
        await retrieve_run(run_id, session=session, pipeline=pipeline)

    assert pipeline.purges == [1]  # a failed phase leaves no downloads behind


async def test_retrieve_run_survives_a_cache_cleanup_failure(session: AsyncSession) -> None:
    class _UnpurgeablePipeline(_FakePipeline):
        async def purge_cache(self) -> int:
            raise OSError("disco de solo lectura")

    run_id = await _ingest_one_turn_with_context(session, "algo")

    await retrieve_run(run_id, session=session, pipeline=_UnpurgeablePipeline())

    # The retrieved context is already persisted; cleanup trouble must not undo it.
    turn = (await session.execute(select(Turn))).scalars().one()
    assert len(turn.retrieved_documents) == 1
    run = await session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == STATUS_EN_RECUPERACION


# The Celery wiring is exercised in tests/test_tasks.py — retrieval no longer owns a task
# of its own now that a run is a chain of per-turn jobs.
