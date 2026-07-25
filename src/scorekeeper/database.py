from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

from scorekeeper.config.settings import get_settings
from scorekeeper.retrieved_context import RetrievedDocument

# JSONB on PostgreSQL, plain JSON on the SQLite fallback.
JsonColumn = JSON().with_variant(JSONB, "postgresql")

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
# expire_on_commit=False is load-bearing under asyncio: with it True, every attribute
# read after a commit is a lazy refresh — i.e. implicit IO from a context that cannot
# do it, which surfaces as MissingGreenlet rather than as a slow query.
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session: AsyncSession | None = None,
) -> AsyncGenerator[AsyncSession]:
    """Yield the injected ``session``, or open a fresh one and close it afterwards.

    The one place the "caller may inject a session, otherwise we own one" convention
    lives; every service function opens with ``async with session_scope(session) as db``.
    ``SessionLocal`` is read from the module global at call time, so tests repoint it
    once and every un-injected caller follows.
    """
    if session is not None:
        yield session
        return
    db = SessionLocal()
    try:
        yield db
    finally:
        await db.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for every model.

    ``AsyncAttrs`` is a safety net, not the mechanism: it allows
    ``await obj.awaitable_attrs.turns`` for a relationship that was not eager-loaded.
    The design is explicit ``selectinload`` at the query (see ``evaluation._run_tree``);
    an ``awaitable_attrs`` in a loop is an N+1 that wants a loader option instead.
    """


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
    # Whether this turn is included in scoring. Opt-in: only turns flagged True are
    # evaluated by the worker (retrieval + LLM judge). Set via the selection endpoint
    # before starting a run. False = skipped for scoring but still part of the
    # conversation history fed to later turns' judges.
    is_selected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # Ground-truth answer for the turn, for reference-based metrics (e.g. contextual
    # precision). Free-form text; None = no reference available for this turn.
    expected_output: Mapped[str | None] = mapped_column(Text, default=None)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    turn_score: Mapped[float | None] = mapped_column(Float, default=None)
    # Raw ``retrieved_context`` cell (ranked source references) captured at ingest; the
    # retrieval pipeline (scorekeeper.retrieval) parses/fetches/extracts it into the
    # ``retrieved_documents`` child rows. None = the sheet had no context column.
    retrieved_context_source: Mapped[str | None] = mapped_column(Text, default=None)

    scenario_result: Mapped[ScenarioResult] = relationship(back_populates="turns")
    # Retrieved context a RAG answer was grounded on, for groundedness-style metrics —
    # one child row per document, ordered by retriever rank. The pydantic
    # ``RetrievedContext`` (scorekeeper.retrieved_context) is the in-memory assembly of
    # these rows. No rows = no retrieved context for this turn.
    retrieved_documents: Mapped[list[RetrievedContextDocument]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
        order_by="RetrievedContextDocument.rank",
    )
    metric_scores: Mapped[list[MetricScore]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
    )
    # LLM token usage for scoring this turn, as its own 1:1 entity.
    token_usage: Mapped[TurnTokenUsage | None] = relationship(
        back_populates="turn",
        uselist=False,
        cascade="all, delete-orphan",
    )


class RetrievedContextDocument(Base):
    """One retrieved document grounding a turn's answer.

    The decoupled, relational form of a ``RetrievedDocument``: one row per document,
    ordered within a turn by ``rank`` (retriever order, 0-based). The in-memory
    ``RetrievedContext`` value object is assembled from these rows.
    """

    __tablename__ = "retrieved_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int | float] = mapped_column(Float)  # retriever order (int or float).
    name: Mapped[str] = mapped_column(Text)  # Short label/title for the retrieved item.
    document: Mapped[str] = mapped_column(Text)  # Source document reference.
    content: Mapped[str] = mapped_column(Text)  # The retrieved text.
    url: Mapped[str | None] = mapped_column(Text, default=None)  # Source URL, if any.

    turn: Mapped[Turn] = relationship(back_populates="retrieved_documents")

    @classmethod
    def from_document(
        cls, doc: RetrievedDocument, rank: int | float
    ) -> "RetrievedContextDocument":
        """Build a row from a pydantic ``RetrievedDocument`` at ``rank``."""
        return cls(
            rank=rank,
            name=doc.name,
            document=doc.document,
            content=doc.content,
            url=doc.url,
        )

    def to_document(self) -> RetrievedDocument:
        """Project this row back into a pydantic ``RetrievedDocument``."""
        return RetrievedDocument(
            name=self.name,
            document=self.document,
            content=self.content,
            url=self.url,
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


class TurnTokenUsage(Base):
    """LLM token usage for scoring one turn (1:1 with ``Turn``).

    Summed across every judge call every metric made while scoring the turn, with
    provider counts normalized to input/output (Anthropic ``input``/``output``,
    OpenAI ``prompt``/``completion``). Kept as its own entity — mirroring
    ``MetricTrace`` — so token/cost accounting stays out of the hot ``turns`` row and
    can grow later (e.g. cost, cached tokens) without widening it. Total is derived
    (``input + output``), never stored.
    """

    __tablename__ = "turn_token_usage"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), unique=True, index=True
    )
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)

    turn: Mapped[Turn] = relationship(back_populates="token_usage")


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


class AuthProviderConfig(Base):
    """Per-provider authentication settings for the retrieval pipeline's authorize stage.

    A single table with a ``provider`` discriminator (the provider *kind*, e.g.
    ``"sharepoint"``) backs the credential taxonomy in
    ``scorekeeper.retrieval.credentials``: one enabled row per gated ``host`` supplies the
    settings its :class:`CredentialProvider` needs to build an authenticated client. The
    certificate ``private_key`` is never stored in the clear — it is encrypted with a
    per-row salt (see ``scorekeeper.retrieval.credentials.secrets``); the plaintext columns
    hold only non-secret identifiers. ``settings`` is kind-specific overflow for future
    providers whose fields do not map onto the SharePoint columns.
    """

    __tablename__ = "auth_providers"
    __table_args__ = (
        UniqueConstraint("provider", "host", name="uq_auth_provider_host"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64))  # credential-provider kind.
    host: Mapped[str] = mapped_column(String(256), index=True)  # gated host this authorizes.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    # SharePoint certificate credentials (non-secret identifiers).
    tenant_id: Mapped[str | None] = mapped_column(String(128), default=None)
    client_id: Mapped[str | None] = mapped_column(String(128), default=None)
    thumbprint: Mapped[str | None] = mapped_column(String(128), default=None)
    site_url: Mapped[str | None] = mapped_column(String(512), default=None)

    # Encrypted certificate private key: Fernet token + its per-row salt (both base64).
    private_key_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    private_key_salt: Mapped[str | None] = mapped_column(Text, default=None)

    # Kind-specific overflow for providers whose fields don't map onto the columns above.
    settings: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    @classmethod
    def from_sharepoint(
        cls,
        *,
        host: str,
        tenant_id: str,
        client_id: str,
        thumbprint: str,
        site_url: str,
        private_key: str,
        encryption_key: str,
        enabled: bool = True,
    ) -> "AuthProviderConfig":
        """Build a ``sharepoint`` row, encrypting ``private_key`` under ``encryption_key``."""
        from scorekeeper.retrieval.credentials.secrets import encrypt_secret

        salt, token = encrypt_secret(private_key, encryption_key)
        return cls(
            provider="sharepoint",
            host=host,
            enabled=enabled,
            tenant_id=tenant_id,
            client_id=client_id,
            thumbprint=thumbprint,
            site_url=site_url,
            private_key_encrypted=token,
            private_key_salt=salt,
        )

    @classmethod
    def from_oauth2(
        cls,
        *,
        host: str,
        client_id: str,
        client_secret: str,
        token_url: str,
        encryption_key: str,
        scope: str | None = None,
        enabled: bool = True,
    ) -> "AuthProviderConfig":
        """Build an ``oauth2`` row, encrypting ``client_secret``.

        The non-secret OAuth2 settings (``token_url``, optional ``scope``) live in the
        ``settings`` JSON overflow; the client secret is encrypted into the shared secret
        columns like any other provider's secret.
        """
        from scorekeeper.retrieval.credentials.secrets import encrypt_secret

        salt, token = encrypt_secret(client_secret, encryption_key)
        settings: dict[str, Any] = {"token_url": token_url}
        if scope is not None:
            settings["scope"] = scope
        return cls(
            provider="oauth2",
            host=host,
            enabled=enabled,
            client_id=client_id,
            private_key_encrypted=token,
            private_key_salt=salt,
            settings=settings,
        )

    def decrypted_secret(self, encryption_key: str) -> str:
        """Decrypt and return the row's stored secret.

        The ``private_key_*`` columns hold whichever secret the provider kind needs — a
        certificate private key (SharePoint), an OAuth2 client secret, … — encrypted with a
        per-row salt. Raises ``SecretError`` when no secret is stored or ``encryption_key`` is
        wrong.
        """
        from scorekeeper.retrieval.credentials.secrets import SecretError, decrypt_secret

        if not self.private_key_encrypted or not self.private_key_salt:
            raise SecretError(f"El proveedor {self.provider} no tiene un secreto almacenado")
        return decrypt_secret(self.private_key_salt, self.private_key_encrypted, encryption_key)

    def decrypted_private_key(self, encryption_key: str) -> str:
        """Alias of :meth:`decrypted_secret`, reading naturally for certificate providers."""
        return self.decrypted_secret(encryption_key)


class DocumentCacheEntry(Base):
    """Index of documents cached on the local filesystem by the fetch stage.

    One row per distinct source ``url`` (unique), pointing at the cached blob under
    ``settings.retrieval_cache_dir``. The fetch stage (``scorekeeper.retrieval.fetch``) reads
    this to avoid re-downloading a document already on disk, so a URL is fetched at most once
    per platform execution even though the pipeline may reference it many times. Standalone —
    no FK into the run hierarchy; the bytes live on disk, not in the DB.

    Rows are **transient**: they are deleted with their blobs once the platform execution's
    retrieval finishes (``evaluation.retrieve_run``), since the extracted markdown on
    ``retrieved_documents`` is the durable record of what was retrieved.
    """

    __tablename__ = "document_cache"
    __table_args__ = (UniqueConstraint("url", name="uq_document_cache_url"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text)  # source document URL (fetch/cache key); unique.
    sha256: Mapped[str] = mapped_column(String(64))  # hex digest of the URL (blob filename).
    cache_path: Mapped[str] = mapped_column(Text)  # blob path relative to the cache root.
    doc_type: Mapped[str] = mapped_column(String(16))  # DocType value of the cached document.
    content_type: Mapped[str | None] = mapped_column(String(255), default=None)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


async def create_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
