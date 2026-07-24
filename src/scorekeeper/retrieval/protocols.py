"""Stage interfaces for the retrieval pipeline.

Each pipeline stage is a ``Protocol`` — a structural contract with no implementation.
Concrete stages (a SharePoint auth provider, a PDF extractor, an httpx-backed fetcher,
…) are supplied in later phases and injected into the orchestrator, mirroring how the
judges take an injected client (``scorekeeper.metrics.judges``). The protocols are
``runtime_checkable`` so tests and the orchestrator can assert conformance.

Data types flowing between stages are defined in ``scorekeeper.retrieval.types``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from scorekeeper.retrieval.types import (
    AuthDecision,
    DocType,
    DocumentLocator,
    ExtractedContent,
    FetchedDocument,
    RetrievalReport,
    SourceFormat,
    SourceRef,
)

if TYPE_CHECKING:
    from scorekeeper.retrieval.credentials.base import AuthClient


@runtime_checkable
class SourceRefParser(Protocol):
    """Parse a ``retrieved_context`` cell into ordered source references."""

    def detect(self, cell: str) -> SourceFormat:
        """Classify how ``cell`` is encoded."""
        ...

    def parse(self, cell: str) -> list[SourceRef]:
        """Parse ``cell`` into ``SourceRef`` objects in retriever-rank order."""
        ...


@runtime_checkable
class DocumentLocatorResolver(Protocol):
    """Resolve a ``SourceRef`` into a concrete fetch target and region."""

    def resolve(self, source: SourceRef) -> DocumentLocator:
        """Derive the document URL, filename, type, host, and page/section."""
        ...


@runtime_checkable
class AuthProvider(Protocol):
    """Decide a document's auth requirement and supply credentials when available."""

    def classify(self, locator: DocumentLocator) -> AuthDecision:
        """Classify the auth requirement/status for ``locator``'s host."""
        ...

    def headers(self, locator: DocumentLocator) -> Mapping[str, str]:
        """Auth headers/cookies for the fetch; empty unless the status is SATISFIED."""
        ...

    def client(self, locator: DocumentLocator) -> AuthClient | None:
        """Generic authenticated client the fetch stage retrieves through.

        ``None`` when the host needs no client (public, or a header-only provider); a
        backend-agnostic :class:`AuthClient` when a credential provider authorizes the host.
        """
        ...


@runtime_checkable
class DocumentFetcher(Protocol):
    """Fetch the raw bytes for a located, authorized document."""

    def fetch(
        self, locator: DocumentLocator, client: AuthClient | None
    ) -> FetchedDocument:
        """Fetch ``locator.document_url`` and return its bytes.

        ``client`` is the authorize stage's generic :class:`AuthClient` for a gated host
        (``AuthProvider.client``); ``None`` for a public host, which is fetched directly.
        """
        ...


@runtime_checkable
class ContentExtractor(Protocol):
    """Select the requested page/section from a fetched document and extract its text."""

    def supports(self, doc_type: DocType) -> bool:
        """Whether this extractor handles ``doc_type``."""
        ...

    def extract(
        self, document: FetchedDocument, locator: DocumentLocator
    ) -> ExtractedContent:
        """Filter ``document`` to ``locator``'s page/section and extract its text."""
        ...


@runtime_checkable
class RetrievalPipeline(Protocol):
    """Orchestrate the stages over one cell into a ``RetrievalReport``."""

    def run(self, cell: str) -> RetrievalReport:
        """Run parse → locate → authorize → fetch → filter → extract → assemble."""
        ...
