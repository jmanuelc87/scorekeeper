"""Concrete Fetch stage: retrieve a located document's bytes, cached on local disk.

``CachingDocumentFetcher`` implements the
:class:`~scorekeeper.core.retrieval.protocols.DocumentFetcher` contract — ``fetch(locator, client)
-> FetchedDocument``. It is the fourth concrete stage of the retrieval pipeline (see
``docs/retrieval-pipeline.md``), between authorize and extract.

How it retrieves depends on what the authorize stage produced for the host:

* a gated host resolves to an :class:`AuthClient` → bytes come from ``client.download(locator)``
  (SharePoint ``ClientContext``, OAuth2 bearer GET, …);
* a public host has no client (``None``) → bytes come from a plain ``httpx2`` GET.

A public GET also **confirms the document type**: the locator's ``DocType`` is derived from
the URL alone (and is merely provisional for an extensionless web page), so the response's
``Content-Type`` overrides it when it names a type we convert. An authenticated download
surfaces no ``Content-Type``, so a gated document keeps the locator's type.

Public GETs are restricted to **globally routable** targets: redirects are followed by hand
so every hop is checked, and a host resolving to a loopback, private, link-local, reserved
or multicast address is refused rather than requested. (The check is resolve-then-request,
so a DNS answer that changes in between is out of scope.)

Every fetch is **cached on the local filesystem** (see ``scorekeeper.core.retrieval.store``): the
first fetch of a ``document_url`` downloads and stores it; later fetches of the same URL read
the blob off disk. The cache dedups *downloads*, not *results* — the fetcher returns one
``FetchedDocument`` per call, so duplicate references to the same URL in a cell each get their
own result (with ``cached=True`` after the first), and only the first touches the network.

The cache is **scoped to a platform execution**, not kept forever: the fetcher remembers every
URL it served and :meth:`CachingDocumentFetcher.purge_cache` drops exactly those blobs (and
their ``document_cache`` rows) when retrieval for that execution finishes — see
``services.retrieval.retrieve_run``. Downloaded documents therefore do not outlive the execution that
needed them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.store import DocumentStore
from scorekeeper.core.retrieval.types import (
    WEB_SCHEMES,
    DocType,
    DocumentLocator,
    FetchedDocument,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from scorekeeper.core.retrieval.credentials.base import AuthClient

logger = logging.getLogger(__name__)

# Default per-request timeout for public HTTP fetches.
_FETCH_TIMEOUT_SECONDS = 30.0

# Redirect hops a public GET follows before giving up (each one is host-checked).
_MAX_REDIRECTS = 5

# Response media types that name a document type we convert. Anything else (or a missing
# header) leaves the locator's type in place.
_MEDIA_TYPES = {
    "text/html": DocType.HTML,
    "application/xhtml+xml": DocType.HTML,
    "application/pdf": DocType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocType.DOCX,
}


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
        session: AsyncSession | None = None,
        http_client: Any | None = None,
    ) -> None:
        root = Path(cache_dir) if cache_dir is not None else Path(get_settings().retrieval_cache_dir)
        self._store = DocumentStore(root, session=session)
        self._http_client = http_client
        # Every document_url served since the last purge — the cleanup scope (see purge_cache).
        self._served: set[str] = set()

    @property
    def http_client(self) -> Any:
        """The HTTP client for public fetches, building a default ``httpx2.Client`` on first use.

        Redirects are **not** followed by the client: ``_download`` walks them itself so each
        hop's target is checked before it is requested.
        """
        if self._http_client is None:
            import httpx2

            self._http_client = httpx2.Client(
                follow_redirects=False, timeout=_FETCH_TIMEOUT_SECONDS
            )
        return self._http_client

    # -- DocumentFetcher protocol -------------------------------------------------------

    async def fetch(
        self, locator: DocumentLocator, client: AuthClient | None
    ) -> FetchedDocument:
        """Return ``locator``'s bytes, from the cache when present else downloading once."""
        self._served.add(locator.document_url)
        cached = await self._store.get(locator.document_url)
        if cached is not None:
            return FetchedDocument(
                document_url=locator.document_url,
                doc_type=cached.doc_type,
                body=cached.body,
                content_type=cached.content_type,
                cached=True,
            )
        # _download drives blocking IO (an authenticated SDK client, or a sync httpx2
        # GET), so it runs on a worker thread rather than stalling the event loop.
        body, content_type = await asyncio.to_thread(self._download, locator, client)
        doc_type = _confirmed_doc_type(content_type, locator.doc_type)
        await self._store.put(
            locator.document_url,
            doc_type=doc_type,
            body=body,
            content_type=content_type,
        )
        return FetchedDocument(
            document_url=locator.document_url,
            doc_type=doc_type,
            body=body,
            content_type=content_type,
            cached=False,
        )

    async def purge_cache(self) -> int:
        """Delete the documents this fetcher served and forget them; return how many went.

        Called when retrieval for a platform execution finishes (``services.retrieval.retrieve_run``):
        the extracted markdown is already persisted on the turns, so the downloaded bytes have
        no further use. Only this fetcher's own URLs are purged, leaving any other execution's
        cache entries alone. Idempotent — a second call with nothing served removes nothing.
        """
        served, self._served = self._served, set()
        if not served:
            return 0
        removed = await self._store.purge(served)
        logger.info("Caché de recuperación: %d documento(s) eliminado(s)", removed)
        return removed

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

        url = locator.document_url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                _ensure_allowed(url)
                response = self.http_client.get(url)
                location = response.headers.get("location") if response.is_redirect else None
                if location is None:
                    response.raise_for_status()
                    return response.content, response.headers.get("content-type")
                url = urljoin(url, location)
        except httpx2.HTTPError as exc:
            raise FetchError(
                f"No se pudo descargar {locator.document_url}: {exc}"
            ) from exc
        raise FetchError(
            f"No se pudo descargar {locator.document_url}: demasiadas redirecciones"
        )


def _confirmed_doc_type(content_type: str | None, provisional: DocType) -> DocType:
    """The type the response claims, falling back to ``provisional``.

    The header wins over the URL — an extensionless page URL arrives here as a provisional
    ``HTML``, and a ``.pdf`` URL that actually served an HTML error page is HTML. An absent
    or unrecognized media type leaves the provisional type alone.
    """
    if not content_type:
        return provisional
    media_type = content_type.split(";", 1)[0].strip().lower()
    return _MEDIA_TYPES.get(media_type, provisional)


def _ensure_allowed(url: str) -> None:
    """Raise ``FetchError`` unless ``url`` is http(s) on a globally routable host.

    Source references come from a model's answer, so a URL naming an internal service
    (``127.0.0.1``, a private range, the cloud metadata endpoint) must not be requested.
    ``is_global`` is False for exactly those: loopback, private, link-local, reserved and
    multicast addresses. The scheme is re-checked here because a redirect can name one the
    orchestrator never saw.
    """
    parts = urlsplit(url)
    if parts.scheme not in WEB_SCHEMES:
        raise FetchError(f"No se pudo descargar {url}: esquema no soportado")
    host = parts.hostname
    if not host:
        raise FetchError(f"No se pudo descargar {url}: la URL no tiene host")
    try:
        addresses = _resolve_addresses(host)
    except OSError as exc:
        raise FetchError(f"No se pudo resolver el host {host}: {exc}") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise FetchError(f"Destino no permitido (red interna): {url}")


def _resolve_addresses(host: str) -> list[str]:
    """Every IP address ``host`` resolves to (the DNS seam tests replace)."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
