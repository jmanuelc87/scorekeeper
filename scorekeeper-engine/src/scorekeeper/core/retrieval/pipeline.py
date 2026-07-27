"""Concrete retrieval orchestrator: thread one cell through every stage.

``RetrievalOrchestrator`` implements the
:class:`~scorekeeper.core.retrieval.protocols.RetrievalPipeline` contract — ``run(cell) ->
RetrievalReport``. It wires the concrete stages (parse → locate → authorize → fetch →
extract → assemble) and turns each source reference into one ``RetrievalOutcome``, catching
each stage's typed error and mapping it onto the matching ``RetrievalStatus`` (best-effort: a
failed reference is recorded, never raised, so the rest of the cell still retrieves).

The auth and fetch stages need a DB session (and the ``auth_encryption_key``), so the
orchestrator is constructed per run with a ``session``; every stage is injectable for tests.

``purge_cache`` closes the loop on the fetch stage's disk cache: the caller invokes it once a
platform execution's turns are retrieved, dropping the downloaded bytes it no longer needs.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.credentials import CredentialError, SecretError, StoredAuthProvider
from scorekeeper.core.retrieval.extract import (
    ExtractError,
    MarkdownContentExtractor,
    PageNotFoundError,
)
from scorekeeper.core.retrieval.fetch import CachingDocumentFetcher, FetchError
from scorekeeper.core.retrieval.parser import LlmSourceRefParser
from scorekeeper.core.retrieval.resolver import UrlDocumentLocatorResolver
from scorekeeper.core.retrieval.types import (
    WEB_SCHEMES,
    AuthStatus,
    RetrievalOutcome,
    RetrievalReport,
    RetrievalStatus,
    SourceRef,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from scorekeeper.core.retrieval.protocols import (
        AuthProvider,
        ContentExtractor,
        DocumentFetcher,
        DocumentLocatorResolver,
        SourceRefParser,
    )

# A whole-cell parse failure has no source reference to attach to; record it against a
# placeholder carrying a snippet of the offending cell.
_PARSE_ERROR_SNIPPET = 200


class RetrievalOrchestrator:
    """Run parse → locate → authorize → fetch → extract → assemble over one cell.

    Defaults build the shipped stages; the DB-backed ones take ``session``/``encryption_key``
    (defaulting to ``settings.auth_encryption_key``). Pass explicit stages to inject fakes.
    """

    def __init__(
        self,
        session: "AsyncSession | None" = None,
        *,
        encryption_key: str | None = None,
        parser: "SourceRefParser | None" = None,
        resolver: "DocumentLocatorResolver | None" = None,
        auth_provider: "AuthProvider | None" = None,
        fetcher: "DocumentFetcher | None" = None,
        extractor: "ContentExtractor | None" = None,
    ) -> None:
        key = encryption_key if encryption_key is not None else get_settings().auth_encryption_key
        self._parser = parser or LlmSourceRefParser()
        self._resolver = resolver or UrlDocumentLocatorResolver()
        self._auth = auth_provider or StoredAuthProvider(session=session, encryption_key=key)
        self._fetcher = fetcher or CachingDocumentFetcher(session=session)
        self._extractor = extractor or MarkdownContentExtractor()

    # -- RetrievalPipeline protocol -----------------------------------------------------

    async def run(self, cell: str) -> RetrievalReport:
        """Parse ``cell`` and retrieve every reference into a ``RetrievalReport``."""
        source_format = self._parser.detect(cell)
        try:
            # The parser drives a blocking LLM call; keep it off the event loop.
            refs = await asyncio.to_thread(self._parser.parse, cell)
        except Exception as exc:  # the (LLM) parse path failed — record one PARSE_ERROR.
            placeholder = SourceRef(name="", url=cell.strip()[:_PARSE_ERROR_SNIPPET], rank=0)
            outcome = RetrievalOutcome(
                source=placeholder,
                status=RetrievalStatus.PARSE_ERROR,
                error=f"No se pudo interpretar la celda: {exc}",
            )
            return RetrievalReport(source_format=source_format, outcomes=[outcome])
        # Sequential on purpose: outcomes keep the reference order of the cell, and the
        # fetch cache dedups best when the same URL is not requested concurrently.
        outcomes = [await self._run_ref(ref) for ref in refs]
        return RetrievalReport(source_format=source_format, outcomes=outcomes)

    async def purge_cache(self) -> int:
        """Drop the documents fetched so far, delegating to the fetch stage's cache."""
        return await self._fetcher.purge_cache()

    # -- per-reference stage threading --------------------------------------------------

    async def _run_ref(self, source: SourceRef) -> RetrievalOutcome:
        """Thread one reference locate → authorize → fetch → extract → assemble."""
        locator = self._resolver.resolve(source)
        if locator.scheme not in WEB_SCHEMES:
            # Not something to fetch (``mailto:``, ``javascript:``, a bare relative path…):
            # stop here so it reads as a bad reference rather than a network failure.
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.UNSUPPORTED_SCHEME,
                locator=locator,
                error=f"esquema no soportado: {locator.scheme or 'sin esquema'}",
            )
        decision = await self._auth.classify(locator)

        if decision.status is AuthStatus.MISSING_CREDENTIALS:
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.AUTH_MISSING,
                locator=locator,
                auth=decision,
                error="faltan credenciales para el host",
            )
        if not self._extractor.supports(locator.doc_type):
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.UNSUPPORTED_TYPE,
                locator=locator,
                auth=decision,
                error=f"tipo de documento no soportado: {locator.doc_type}",
            )

        try:
            client = await self._auth.client(locator)
        except (CredentialError, SecretError) as exc:  # gated host, client won't build
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.AUTH_MISSING,
                locator=locator,
                auth=decision,
                error=str(exc),
            )

        try:
            document = await self._fetcher.fetch(locator, client)
        except FetchError as exc:
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.FETCH_FAILED,
                locator=locator,
                auth=decision,
                error=str(exc),
            )

        try:
            # markitdown/pypdf conversion is blocking CPU work.
            extracted = await asyncio.to_thread(self._extractor.extract, document, locator)
        except PageNotFoundError as exc:
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.LOCATOR_NOT_FOUND,
                locator=locator,
                auth=decision,
                error=str(exc),
            )
        except ExtractError as exc:  # corrupt/convert failure (no dedicated status)
            return RetrievalOutcome(
                source=source,
                status=RetrievalStatus.FETCH_FAILED,
                locator=locator,
                auth=decision,
                error=str(exc),
            )

        # RETRIEVED (or EMPTY_CONTENT when the extracted markdown is blank).
        return RetrievalOutcome.assembled(source, locator, extracted, auth=decision)
