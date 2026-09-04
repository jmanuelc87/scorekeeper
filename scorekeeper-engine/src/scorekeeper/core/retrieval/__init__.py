"""Retrieved-context retrieval pipeline.

This package turns the ranked *source references* stored in a turn's
``retrieved_context`` cell (a label + URL, sometimes with a ``#page=N`` fragment) into
the ``RetrievedContext`` schema by fetching and extracting the referenced document text::

    parse -> locate -> authorize -> fetch -> filter -> extract -> assemble

Currently this package defines only the **taxonomy**: the enums and pydantic value
objects in :mod:`scorekeeper.core.retrieval.types` and the stage ``Protocol`` interfaces in
:mod:`scorekeeper.core.retrieval.protocols`. The concrete stage implementations, the
credential providers, the document extractors, and the run wiring are added in later
phases against these types. See ``docs/retrieval-pipeline.md``.
"""

from __future__ import annotations

from scorekeeper.core.retrieval.auth import HostRuleAuthProvider
from scorekeeper.core.retrieval.credentials import (
    AuthClient,
    CredentialError,
    CredentialProvider,
    StoredAuthProvider,
    register_credential_provider,
)
from scorekeeper.core.retrieval.extract import (
    ExtractError,
    MarkdownContentExtractor,
    PageNotFoundError,
    default_content_extractor,
)
from scorekeeper.core.retrieval.extract_unstructured import UnstructuredContentExtractor
from scorekeeper.core.retrieval.fetch import CachingDocumentFetcher, FetchError
from scorekeeper.core.retrieval.parser import LlmSourceRefParser
from scorekeeper.core.retrieval.pipeline import RetrievalOrchestrator
from scorekeeper.core.retrieval.protocols import (
    AuthProvider,
    ContentExtractor,
    DocumentFetcher,
    DocumentLocatorResolver,
    RetrievalPipeline,
    SourceRefParser,
)
from scorekeeper.core.retrieval.resolver import UrlDocumentLocatorResolver
from scorekeeper.core.retrieval.types import (
    STATUS_EN_RECUPERACION,
    AuthDecision,
    AuthRequirement,
    AuthStatus,
    DocType,
    DocumentLocator,
    ExtractedContent,
    FetchedDocument,
    RetrievalOutcome,
    RetrievalReport,
    RetrievalStatus,
    RetrievalSummary,
    SourceFormat,
    SourceRef,
)

__all__ = [
    "STATUS_EN_RECUPERACION",
    # Enums
    "SourceFormat",
    "DocType",
    "AuthRequirement",
    "AuthStatus",
    "RetrievalStatus",
    # Models
    "SourceRef",
    "DocumentLocator",
    "AuthDecision",
    "FetchedDocument",
    "ExtractedContent",
    "RetrievalOutcome",
    "RetrievalReport",
    "RetrievalSummary",
    # Stage protocols
    "SourceRefParser",
    "DocumentLocatorResolver",
    "AuthProvider",
    "DocumentFetcher",
    "ContentExtractor",
    "RetrievalPipeline",
    # Stage implementations
    "LlmSourceRefParser",
    "UrlDocumentLocatorResolver",
    "HostRuleAuthProvider",
    "StoredAuthProvider",
    "CachingDocumentFetcher",
    "FetchError",
    "MarkdownContentExtractor",
    "UnstructuredContentExtractor",
    "default_content_extractor",
    "ExtractError",
    "PageNotFoundError",
    "RetrievalOrchestrator",
    # Credential taxonomy
    "AuthClient",
    "CredentialProvider",
    "CredentialError",
    "register_credential_provider",
]
