from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from scorekeeper.config import get_settings

# JSONB on PostgreSQL, plain JSON on the SQLite fallback.
JsonColumn = JSON().with_variant(JSONB, "postgresql")

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SourceFile(Base):
    """An imported .xlsx file of interactions — the source of one or more runs."""

    __tablename__ = "source_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(512))
    file_hash: Mapped[str] = mapped_column(String(128), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Sheet names, row counts, column mapping, etc. — whatever the importer records.
    sheet_metadata: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)

    runs: Mapped[list[BenchmarkRun]] = relationship(back_populates="source_file")


class BenchmarkRun(Base):
    """A single benchmark invocation, scored under one platform.

    The ``platform_executions`` relationship is a list for historical reasons, but the
    orchestrator now creates exactly one ``PlatformExecution`` per run — one platform
    per set of files.
    """

    __tablename__ = "benchmark_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL"), index=True, default=None
    )

    source_file: Mapped[SourceFile | None] = relationship(back_populates="runs")
    platform_executions: Mapped[list[PlatformExecution]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PlatformExecution.started_at",
    )


class PlatformExecution(Base):
    """Results for one platform (Copilot, Gemini, Claude) within a run."""

    __tablename__ = "platform_executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    average_score: Mapped[float | None] = mapped_column(Float, default=None)

    run: Mapped[BenchmarkRun] = relationship(back_populates="platform_executions")
    scenario_results: Mapped[list[ScenarioResult]] = relationship(
        back_populates="platform_execution",
        cascade="all, delete-orphan",
    )


class ScenarioResult(Base):
    """A single conversation (use case) loaded from the source file and scored."""

    __tablename__ = "scenario_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    platform_execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_executions.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(String(128))
    use_case: Mapped[str] = mapped_column(
        String(128), default="default", server_default="default"
    )
    # Provenance of the conversation: a sheet name, conversation key or row range
    # for a file import, or the full chat URL for a live browser capture. Unbounded
    # because a captured URL (Copilot threads carry request ids and origin params)
    # runs well past any column width worth guessing at.
    source_ref: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    screenshot_path: Mapped[str | None] = mapped_column(String(512), default=None)
    average_score: Mapped[float | None] = mapped_column(Float, default=None)
    # Parsed rows for this conversation from the source file; Turn rows are the
    # evaluation projection derived from it.
    raw_conversation: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)

    platform_execution: Mapped[PlatformExecution] = relationship(
        back_populates="scenario_results"
    )
    turns: Mapped[list[Turn]] = relationship(
        back_populates="scenario_result",
        cascade="all, delete-orphan",
        order_by="Turn.turn_number",
    )


class Turn(Base):
    """One user/model exchange in a conversation, evaluated on its own."""

    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scenario_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scenario_results.id", ondelete="CASCADE"), index=True
    )
    turn_number: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text)
    # Retrieved context a RAG answer was grounded on, for groundedness-style
    # metrics. Free-form text blob; None = not applicable to this turn.
    retrieved_context: Mapped[str | None] = mapped_column(Text, default=None)
    # Ground-truth answer for the turn, for reference-based metrics (e.g. contextual
    # precision). Free-form text; None = no reference available for this turn.
    expected_output: Mapped[str | None] = mapped_column(Text, default=None)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    turn_score: Mapped[float | None] = mapped_column(Float, default=None)

    scenario_result: Mapped[ScenarioResult] = relationship(back_populates="turns")
    metric_scores: Mapped[list[MetricScore]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
    )


class MetricScore(Base):
    """An LLM-as-a-judge score for a single metric on a single turn."""

    __tablename__ = "metric_scores"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), index=True
    )
    metric_name: Mapped[str] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float)
    judge_model: Mapped[str | None] = mapped_column(String(128), default=None)
    rubric_version: Mapped[str | None] = mapped_column(String(64), default=None)

    turn: Mapped[Turn] = relationship(back_populates="metric_scores")
    # Structured record of what the metric produced for this turn, as its own 1:1
    # entity (replaces the former flattened Spanish justification string).
    trace: Mapped[MetricTrace | None] = relationship(
        back_populates="metric_score",
        uselist=False,
        cascade="all, delete-orphan",
    )


class MetricTrace(Base):
    """The structured trace a metric produced for one turn (1:1 with MetricScore).

    Distinct from the Pydantic ``scorekeeper.metrics.base.MetricTrace`` domain
    model — this is its persisted mirror. ``steps`` holds the same list the domain
    model's ``steps`` field carries (each step: label/summary/entries).
    """

    __tablename__ = "metric_traces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    metric_score_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metric_scores.id", ondelete="CASCADE"), unique=True, index=True
    )
    # The list of steps (each: label/summary/entries) as JSON; the same payload the
    # domain MetricTrace.steps carries.
    steps: Mapped[list[Any] | None] = mapped_column(JsonColumn, default=None)

    metric_score: Mapped[MetricScore] = relationship(back_populates="trace")


class ScenarioMetric(Base):
    """Which metric applies to which scenario ``use_case``.

    The metric taxonomy lives in code (see ``scorekeeper.metrics``); this table is
    the queryable projection of each metric's decorator-declared scenarios,
    materialized by ``scorekeeper.metrics.selection.sync_selection``. The scoring
    runner reads it to pick the metric subset for a scenario. ``metric_name`` is a
    plain string validated against the code registry (no FK, since there is no
    metric-definitions table). ``use_case == "default"`` is the fallback set.
    """

    __tablename__ = "scenario_metrics"
    __table_args__ = (UniqueConstraint("use_case", "metric_name", name="uq_scenario_metric"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    use_case: Mapped[str] = mapped_column(String(128), index=True)
    metric_name: Mapped[str] = mapped_column(String(128))


def create_schema() -> None:
    Base.metadata.create_all(engine)
