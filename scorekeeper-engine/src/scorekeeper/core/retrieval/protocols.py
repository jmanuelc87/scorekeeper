"""Stage interfaces for the retrieval pipeline.

Each pipeline stage is a ``Protocol`` — a structural contract with no implementation.
Concrete stages (a SharePoint auth provider, a PDF extractor, an httpx-backed fetcher,
…) are supplied in later phases and injected into the orchestrator, mirroring how the
judges take an injected client (``scorekeeper.core.metrics.judges``). The protocols are
``runtime_checkable`` so tests and the orchestrator can assert conformance.

Data types flowing between stages are defined in ``scorekeeper.core.retrieval.types``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from scorekeeper.core.retrieval.types import (
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
    from scorekeeper.core.retrieval.credentials.base import AuthClient


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

    async def classify(self, locator: DocumentLocator) -> AuthDecision:
        """Classify the auth requirement/status for ``locator``'s host."""
        ...

    def headers(self, locator: DocumentLocator) -> Mapping[str, str]:
        """Auth headers/cookies for the fetch; empty unless the status is SATISFIED."""
        ...

    async def client(self, locator: DocumentLocator) -> AuthClient | None:
        """Generic authenticated client the fetch stage retrieves through.

        ``None`` when the host needs no client (public, or a header-only provider); a
        backend-agnostic :class:`AuthClient` when a credential provider authorizes the host.
        """
        ...


@runtime_checkable
class DocumentFetcher(Protocol):
    """Fetch the raw bytes for a located, authorized document."""

    async def fetch(
        self, locator: DocumentLocator, client: AuthClient | None
    ) -> FetchedDocument:
        """Fetch ``locator.document_url`` and return its bytes.

        ``client`` is the authorize stage's generic :class:`AuthClient` for a gated host
        (``AuthProvider.client``); ``None`` for a public host, which is fetched directly.
        """
        ...

    async def purge_cache(self) -> int:
        """Drop the documents fetched so far and return how many were removed.

        Called once retrieval for a platform execution finishes, so downloaded bytes do not
        outlive the execution that needed them. A fetcher that caches nothing returns 0.
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

    async def run(self, cell: str) -> RetrievalReport:
        """Run parse → locate → authorize → fetch → filter → extract → assemble."""
        ...

    async def purge_cache(self) -> int:
        """Release the documents retrieved so far (see ``DocumentFetcher.purge_cache``)."""
        ...
