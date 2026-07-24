"""Tests for the retrieval+scoring orchestration: retrieve_run and the Celery wiring."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scorekeeper import evaluation, tasks
from scorekeeper.database import Base, BenchmarkRun, Turn
from scorekeeper.evaluation import UploadedFile, ingest_evaluation, retrieve_run
from scorekeeper.retrieval.types import (
    STATUS_EN_RECUPERACION,
    DocType,
    DocumentLocator,
    ExtractedContent,
    RetrievalOutcome,
    RetrievalReport,
    SourceFormat,
    SourceRef,
)
from scorekeeper.runner import STATUS_FALLIDO


class _FakePipeline:
    """A RetrievalPipeline stub: each cell yields one RETRIEVED doc echoing the cell text."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self._raise = raise_exc
        self.calls: list[str] = []
        # Cells retrieved at the time of each purge_cache() call — lets a test assert both
        # how many times the cache was released and that it happened after the turns ran.
        self.purges: list[int] = []

    def purge_cache(self) -> int:
        self.purges.append(len(self.calls))
        return len(self.calls)

    def run(self, cell: str) -> RetrievalReport:
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


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _xlsx(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _ingest_one_turn_with_context(session: Session, cell: str) -> str:
    content = _xlsx(
        ["turn", "role", "content", "retrieved_context"],
        [[1, "user", "pregunta", cell], [1, "model", "respuesta", ""]],
    )
    files = [UploadedFile(filename="esc.xlsx", content=content, scenario_id="esc")]
    return ingest_evaluation("claude", files, session=session)


# -- retrieve_run --------------------------------------------------------------------------


def test_retrieve_run_populates_documents_and_sets_status(session: Session) -> None:
    run_id = _ingest_one_turn_with_context(session, "manual (https://h/x.html)")
    pipeline = _FakePipeline()

    retrieve_run(run_id, session=session, pipeline=pipeline)

    turn = session.execute(select(Turn)).scalars().one()
    assert [d.content for d in turn.retrieved_documents] == ["md::manual (https://h/x.html)"]
    # Retrieval leaves the run in the retrieval phase; scoring advances it afterwards.
    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == STATUS_EN_RECUPERACION
    assert pipeline.calls == ["manual (https://h/x.html)"]


def test_retrieve_run_skips_turns_without_source(session: Session) -> None:
    # No retrieved_context column at all → no source, pipeline never runs.
    content = _xlsx(["turn", "role", "content"], [[1, "user", "hola"], [1, "model", "hey"]])
    files = [UploadedFile(filename="esc.xlsx", content=content, scenario_id="esc")]
    run_id = ingest_evaluation("claude", files, session=session)
    pipeline = _FakePipeline()

    retrieve_run(run_id, session=session, pipeline=pipeline)

    turn = session.execute(select(Turn)).scalars().one()
    assert turn.retrieved_documents == []
    assert pipeline.calls == []


def test_retrieve_run_hard_failure_marks_fallido(session: Session) -> None:
    run_id = _ingest_one_turn_with_context(session, "algo")
    pipeline = _FakePipeline(raise_exc=RuntimeError("kaboom"))

    with pytest.raises(RuntimeError):
        retrieve_run(run_id, session=session, pipeline=pipeline)

    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == STATUS_FALLIDO


def test_retrieve_run_is_idempotent(session: Session) -> None:
    run_id = _ingest_one_turn_with_context(session, "algo")
    retrieve_run(run_id, session=session, pipeline=_FakePipeline())
    retrieve_run(run_id, session=session, pipeline=_FakePipeline())  # re-run
    turn = session.execute(select(Turn)).scalars().one()
    assert len(turn.retrieved_documents) == 1  # cleared + repopulated, not doubled


# -- cache cleanup -------------------------------------------------------------------------


def test_retrieve_run_purges_cache_once_per_platform_execution(session: Session) -> None:
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
    run_id = ingest_evaluation("claude", files, session=session)
    pipeline = _FakePipeline()

    retrieve_run(run_id, session=session, pipeline=pipeline)

    assert pipeline.calls == ["ref-1", "ref-2"]
    assert pipeline.purges == [2]  # one purge, after both scenarios ran


def test_retrieve_run_purges_cache_on_failure(session: Session) -> None:
    run_id = _ingest_one_turn_with_context(session, "algo")
    pipeline = _FakePipeline(raise_exc=RuntimeError("kaboom"))

    with pytest.raises(RuntimeError):
        retrieve_run(run_id, session=session, pipeline=pipeline)

    assert pipeline.purges == [1]  # a failed phase leaves no downloads behind


def test_retrieve_run_survives_a_cache_cleanup_failure(session: Session) -> None:
    class _UnpurgeablePipeline(_FakePipeline):
        def purge_cache(self) -> int:
            raise OSError("disco de solo lectura")

    run_id = _ingest_one_turn_with_context(session, "algo")

    retrieve_run(run_id, session=session, pipeline=_UnpurgeablePipeline())

    # The retrieved context is already persisted; cleanup trouble must not undo it.
    turn = session.execute(select(Turn)).scalars().one()
    assert len(turn.retrieved_documents) == 1
    run = session.get(BenchmarkRun, uuid.UUID(run_id))
    assert run.status == STATUS_EN_RECUPERACION


# -- Celery wiring -------------------------------------------------------------------------


def test_run_pipeline_task_runs_retrieval_before_scoring(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(evaluation, "retrieve_run", lambda rid: order.append("retrieve"))
    monkeypatch.setattr(evaluation, "score_run", lambda rid: order.append("score"))

    tasks.run_pipeline_task("run-123")

    assert order == ["retrieve", "score"]


def test_enqueue_run_delegates_to_task(monkeypatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(tasks.run_pipeline_task, "delay", lambda rid: captured.update(id=rid))

    tasks.enqueue_run("run-abc")

    assert captured == {"id": "run-abc"}
