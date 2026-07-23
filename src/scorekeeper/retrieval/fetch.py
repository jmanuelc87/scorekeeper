"""Concrete Fetch stage: retrieve a located document's bytes, cached on local disk.

``CachingDocumentFetcher`` implements the
:class:`~scorekeeper.retrieval.protocols.DocumentFetcher` contract — ``fetch(locator, client)
-> FetchedDocument``. It is the fourth concrete stage of the retrieval pipeline (see
``docs/retrieval-pipeline.md``), between authorize and extract.

How it retrieves depends on what the authorize stage produced for the host:

* a gated host resolves to an :class:`AuthClient` → bytes come from ``client.download(locator)``
  (SharePoint ``ClientContext``, OAuth2 bearer GET, …);
* a public host has no client (``None``) → bytes come from a plain ``httpx2`` GET.

Every fetch is **cached on the local filesystem** (see ``scorekeeper.retrieval.store``): the
first fetch of a ``document_url`` downloads and stores it; later fetches of the same URL read
the blob off disk. The cache dedups *downloads*, not *results* — the fetcher returns one
``FetchedDocument`` per call, so duplicate references to the same URL in a cell each get their
own result (with ``cached=True`` after the first), and only the first touches the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from scorekeeper.config import get_settings
from scorekeeper.retrieval.store import DocumentStore
from scorekeeper.retrieval.types import DocumentLocator, FetchedDocument

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from scorekeeper.retrieval.credentials.base import AuthClient

# Default per-request timeout for public HTTP fetches.
_FETCH_TIMEOUT_SECONDS = 30.0


class FetchError(Exception):
    """Raised when a document cannot be downloaded (network / HTTP error).

    A future orchestrator maps this onto ``RetrievalStatus.FETCH_FAILED``.
    """


class CachingDocumentFetcher:
    """Fetch document bytes through the generic ``AuthClient`` (or a public GET), disk-cached.

    ``cache_dir`` defaults to ``settings.retrieval_cache_dir`` and ``session`` to a fresh
    ``SessionLocal()`` per cache operation (tests inject an in-memory session). ``http_client``
    (an httpx-style client with ``get``) is injectable; by default an ``httpx2.Client`` is built
    lazily, so public fetches need no wiring and gated fetches need no HTTP client at all.
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        session: Session | None = None,
        http_client: Any | None = None,
    ) -> None:
        root = Path(cache_dir) if cache_dir is not None else Path(get_settings().retrieval_cache_dir)
        self._store = DocumentStore(root, session=session)
        self._http_client = http_client

    @property
    def http_client(self) -> Any:
        """The HTTP client for public fetches, building a default ``httpx2.Client`` on first use."""
        if self._http_client is None:
            import httpx2

            self._http_client = httpx2.Client(
                follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS
            )
        return self._http_client

    # -- DocumentFetcher protocol -------------------------------------------------------

    def fetch(
        self, locator: DocumentLocator, client: AuthClient | None
    ) -> FetchedDocument:
        """Return ``locator``'s bytes, from the cache when present else downloading once."""
        cached = self._store.get(locator.document_url)
        if cached is not None:
            return FetchedDocument(
                document_url=locator.document_url,
                doc_type=cached.doc_type,
                body=cached.body,
                content_type=cached.content_type,
                cached=True,
            )
        body, content_type = self._download(locator, client)
        self._store.put(
            locator.document_url,
            doc_type=locator.doc_type,
            body=body,
            content_type=content_type,
        )
        return FetchedDocument(
            document_url=locator.document_url,
            doc_type=locator.doc_type,
            body=body,
            content_type=content_type,
            cached=False,
        )

    # -- helpers ------------------------------------------------------------------------

    def _download(
        self, locator: DocumentLocator, client: AuthClient | None
    ) -> tuple[bytes, str | None]:
        """Retrieve bytes via the auth client (gated) or a public GET; raise ``FetchError``."""
        if client is not None:
            # The generic client fetches through its authenticated backend; it does not
            # surface a Content-Type, so leave it unknown for auth'd fetches.
            return client.download(locator), None

        import httpx2

        try:
            response = self.http_client.get(locator.document_url)
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            raise FetchError(
                f"No se pudo descargar {locator.document_url}: {exc}"
            ) from exc
        return response.content, response.headers.get("content-type")
