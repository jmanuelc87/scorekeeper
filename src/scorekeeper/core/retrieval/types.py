"""Taxonomy for the retrieved-context retrieval pipeline.

The ``retrieved_context`` cell of a source ``.xlsx`` does not carry document text — it
carries a ranked list of *source references* (a label + a URL, sometimes with a
``#page=N`` fragment and an index). Turning those references into the stored
``RetrievedContext`` schema is a multi-stage pipeline::

    parse -> locate -> authorize -> fetch -> filter -> extract -> assemble

This module defines the **type system** for that pipeline — the enums, the per-document
outcome/status taxonomy, and the pydantic value objects that flow between stages. The
stage *implementations* live behind the ``Protocol`` interfaces in
``scorekeeper.core.retrieval.protocols`` and are added in later phases; this module has no
network, PDF, HTML, or auth logic. The only behaviour here is the trivial, pure
assembly helpers ``RetrievalReport.to_context`` and ``RetrievalSummary.from_outcomes``.

Every stage ultimately targets the storage contract in
``scorekeeper.core.retrieved_context``: ``{"documents": [{name, document, content, url}, ...]}``
ordered by retriever rank.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel

from scorekeeper.core.retrieved_context import RetrievedContext, RetrievedDocument

# Run-level status a benchmark run carries while its documents are being retrieved,
# so ``GET /evaluations`` can surface the retrieval phase. Defined here alongside the
# taxonomy; ``services.status.STATUS_*`` and the API serializer adopt it in a later phase.
# Follow-on rollup states (documented, not yet emitted): ``recuperacion_parcial`` when
# some documents failed and ``recuperacion_fallida`` when the phase itself failed.
STATUS_EN_RECUPERACION = "en_recuperacion"


class SourceFormat(StrEnum):
    """How the ``retrieved_context`` cell was encoded in the spreadsheet."""

    PIPE_LABELLED = "pipe_labelled"  # ``name (url) | name (url) | ...``
    JSON_NAME_URL = "json_name_url"  # ``[{"name", "url"}, ...]``
    JSON_INDEXED = "json_indexed"  # ``[{"index", "url", "name"}, ...]``
    PLAINTEXT = "plaintext"  # free-form blob (legacy fallback)
    EMPTY = "empty"  # blank cell


class DocType(StrEnum):
    """The kind of document a source URL points at."""

    PDF = "pdf"
    DOCX = "docx"  # Word (.docx); legacy binary .doc is not supported.
    HTML = "html"
    UNKNOWN = "unknown"


class AuthRequirement(StrEnum):
    """Whether fetching a document requires authentication."""

    PUBLIC = "public"  # retrievable without credentials
    REQUIRED = "required"  # the host gates the document behind auth


class AuthStatus(StrEnum):
    """Resolution of a document's auth requirement against available credentials.

    The three real-world cases: no auth needed → retrieve; auth needed and we hold
    credentials → retrieve; auth needed and we lack credentials → cannot retrieve.
    """

    NOT_NEEDED = "not_needed"  # AuthRequirement.PUBLIC
    SATISFIED = "satisfied"  # REQUIRED and credentials available
    MISSING_CREDENTIALS = "missing_credentials"  # REQUIRED and no credentials


class RetrievalStatus(StrEnum):
    """Terminal outcome of retrieving one source reference."""

    PENDING = "pending"  # not yet processed
    RETRIEVED = "retrieved"  # content extracted successfully
    AUTH_MISSING = "auth_missing"  # auth required, no credentials
    FETCH_FAILED = "fetch_failed"  # network / HTTP error
    UNSUPPORTED_TYPE = "unsupported_type"  # no extractor for the doc type
    LOCATOR_NOT_FOUND = "locator_not_found"  # requested page/section absent
    EMPTY_CONTENT = "empty_content"  # extracted, but no usable text
    PARSE_ERROR = "parse_error"  # the source reference could not be parsed


class SourceRef(BaseModel):
    """A normalized source reference parsed from the cell (Parse-stage output)."""

    name: str  # Label from the cell (host or title, e.g. "cognitactix-my.sharepoint.com").
    url: str  # The raw source URL, fragment included.
    rank: int  # Position in the cell's list; preserves retriever order.
    index: str | None = None  # Provider index token (format 3, e.g. "1-abc").


class DocumentLocator(BaseModel):
    """Where and what to fetch, resolved from a ``SourceRef`` (Locate-stage output)."""

    document_url: str  # URL without the fragment — the fetch / cache key.
    filename: str  # Filename derived from the URL path (e.g. "ReporteFinanciero.pdf").
    doc_type: DocType
    host: str  # URL netloc, used to classify the auth requirement.
    page: int | None = None  # Page from a ``#page=N`` fragment, if any.
    section: str | None = None  # Section/anchor within the document, if any.


class AuthDecision(BaseModel):
    """The auth requirement for a document and its resolution (Authorize-stage output)."""

    requirement: AuthRequirement
    status: AuthStatus
    provider: str | None = None  # Identifier of the rule/provider that matched.


class FetchedDocument(BaseModel):
    """Raw bytes fetched for a document (Fetch-stage output; never persisted)."""

    model_config = {"arbitrary_types_allowed": True}

    document_url: str
    doc_type: DocType
    body: bytes
    content_type: str | None = None  # HTTP ``Content-Type`` header, if present.
    cached: bool = False  # True when served from the local cache (no network fetch).


class ExtractedContent(BaseModel):
    """Text selected for the requested page/section (Filter+Extract-stage output)."""

    text: str
    page: int | None = None

    def to_document(
        self, source: "SourceRef", locator: "DocumentLocator"
    ) -> RetrievedDocument:
        """Assemble a ``RetrievedDocument`` from this content and its source reference.

        Pure field mapping (Assemble stage). ``name`` is the reference label; ``document`` is a
        human source ref (the filename, falling back to the label then the URL); ``content`` is
        the extracted markdown; ``url`` keeps ``source.url`` so any ``#page=N`` citation anchor
        survives (unlike the fragment-stripped ``document_url`` fetch key).
        """
        return RetrievedDocument(
            name=source.name,
            document=locator.filename or source.name or locator.document_url,
            content=self.text,
            url=source.url or None,
        )


class RetrievalOutcome(BaseModel):
    """One source reference threaded through the whole pipeline.

    ``document`` is populated only when ``status`` is ``RETRIEVED``; ``error`` carries a
    short diagnostic for the failure statuses.
    """

    source: SourceRef
    status: RetrievalStatus = RetrievalStatus.PENDING
    locator: DocumentLocator | None = None
    auth: AuthDecision | None = None
    document: RetrievedDocument | None = None
    error: str | None = None

    @classmethod
    def assembled(
        cls,
        source: SourceRef,
        locator: DocumentLocator,
        extracted: ExtractedContent,
        *,
        auth: AuthDecision | None = None,
    ) -> "RetrievalOutcome":
        """Assemble one reference's terminal outcome from its extract result.

        Non-blank markdown → ``RETRIEVED`` carrying the assembled ``RetrievedDocument``; blank
        text → ``EMPTY_CONTENT`` with no document (so ``to_context`` drops it). ``locator`` and
        the optional ``auth`` are threaded through for the orchestrator.
        """
        if extracted.text.strip():
            return cls(
                source=source,
                status=RetrievalStatus.RETRIEVED,
                locator=locator,
                auth=auth,
                document=extracted.to_document(source, locator),
            )
        return cls(
            source=source,
            status=RetrievalStatus.EMPTY_CONTENT,
            locator=locator,
            auth=auth,
            error="contenido vacío",
        )


class RetrievalReport(BaseModel):
    """The result of running the pipeline over one ``retrieved_context`` cell."""

    source_format: SourceFormat
    outcomes: list[RetrievalOutcome] = []

    def to_context(self) -> RetrievedContext:
        """Assemble the successfully-retrieved documents into a ``RetrievedContext``.

        Keeps the outcomes' order — assemble assumes they are supplied in retriever-rank order
        (as the parser/orchestrator produce them) — and includes only outcomes whose ``status``
        is ``RETRIEVED`` with a populated ``document`` (built by ``RetrievalOutcome.assembled``).
        Duplicate references to the same URL are preserved as separate documents. Pure — no I/O.
        """
        return RetrievedContext(
            documents=[
                outcome.document
                for outcome in self.outcomes
                if outcome.status is RetrievalStatus.RETRIEVED and outcome.document is not None
            ]
        )


class RetrievalSummary(BaseModel):
    """Per-status counts over a set of outcomes, for ``GET /evaluations`` reporting."""

    total: int = 0
    retrieved: int = 0
    auth_missing: int = 0
    fetch_failed: int = 0
    unsupported: int = 0
    other: int = 0  # PENDING / LOCATOR_NOT_FOUND / EMPTY_CONTENT / PARSE_ERROR

    @classmethod
    def from_outcomes(cls, outcomes: Iterable[RetrievalOutcome]) -> "RetrievalSummary":
        """Tally ``outcomes`` by ``RetrievalStatus``. Pure — no I/O."""
        summary = cls()
        for outcome in outcomes:
            summary.total += 1
            match outcome.status:
                case RetrievalStatus.RETRIEVED:
                    summary.retrieved += 1
                case RetrievalStatus.AUTH_MISSING:
                    summary.auth_missing += 1
                case RetrievalStatus.FETCH_FAILED:
                    summary.fetch_failed += 1
                case RetrievalStatus.UNSUPPORTED_TYPE:
                    summary.unsupported += 1
                case _:
                    summary.other += 1
        return summary
