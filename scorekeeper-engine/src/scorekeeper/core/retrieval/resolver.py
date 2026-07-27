"""Concrete Locate stage: resolve a ``SourceRef`` into a concrete fetch target.

``UrlDocumentLocatorResolver`` implements the
:class:`~scorekeeper.core.retrieval.protocols.DocumentLocatorResolver` contract —
``resolve(source) -> DocumentLocator``. It is the second concrete stage of the retrieval
pipeline (see ``docs/retrieval-pipeline.md``), sitting between parse and authorize; the
later stages (authorize/fetch/extract) remain deferred.

Resolution is **pure and deterministic**: ``urllib.parse`` only, no network, no PDF/HTML
parsing, no LLM, and no credentials. From a ``SourceRef``'s raw URL it derives the fetch
key (URL minus fragment), the filename, the :class:`DocType`, the scheme, the host, and
the page/section requested by a ``#page=N`` fragment.

The ``DocType`` of an **extensionless http(s) URL** — an ordinary web page such as
``https://eleconomista.com.mx/noticias/`` — is a *provisional* ``HTML``: the URL alone says
nothing, so the fetch stage confirms the real type from the response ``Content-Type``.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urldefrag, urlsplit

from scorekeeper.core.retrieval.types import WEB_SCHEMES, DocType, DocumentLocator, SourceRef

# A ``page=N`` token anywhere in the URL fragment (e.g. ``#page=3``) — the page selector.
_PAGE_FRAGMENT = re.compile(r"page=(\d+)")

# Filename extensions mapped to the document type they denote.
_EXTENSION_TYPES = {
    ".pdf": DocType.PDF,
    ".docx": DocType.DOCX,  # legacy binary .doc is intentionally unmapped (→ UNKNOWN).
    ".htm": DocType.HTML,
    ".html": DocType.HTML,
}


class UrlDocumentLocatorResolver:
    """Resolve a ``SourceRef`` into a ``DocumentLocator`` using only ``urllib.parse``.

    Stateless and side-effect-free: ``resolve`` derives every field from ``source.url``.
    """

    # -- DocumentLocatorResolver protocol -----------------------------------------------

    def resolve(self, source: SourceRef) -> DocumentLocator:
        """Derive the document URL, filename, type, scheme, host, and page/section from ``source``.

        The fragment is stripped for ``document_url`` (the fetch/cache key) but mined for
        the requested ``page``/``section``; the query string is preserved in the URL and
        ignored for the filename.
        """
        document_url, fragment = urldefrag(source.url)
        parts = urlsplit(source.url)
        scheme = parts.scheme.lower()
        filename = _filename_from_path(parts.path)
        page, section = _page_and_section(fragment)
        return DocumentLocator(
            document_url=document_url,
            filename=filename,
            doc_type=_doc_type_for(filename, scheme),
            scheme=scheme,
            host=parts.netloc.lower(),
            page=page,
            section=section,
        )


def _filename_from_path(path: str) -> str:
    """Return the URL-unquoted final path segment (``""`` when there is none)."""
    last_segment = path.rsplit("/", 1)[-1]
    return unquote(last_segment)


def _doc_type_for(filename: str, scheme: str) -> DocType:
    """Classify ``filename`` by its lowercased extension.

    An unrecognized extension (e.g. legacy ``.doc``) is ``UNKNOWN``. *No* extension is not
    the same case: an http(s) URL without one is an ordinary web page, so it gets a
    provisional ``HTML`` the fetch stage confirms from the response ``Content-Type``.
    """
    _, dot, extension = filename.rpartition(".")
    if dot and extension:
        return _EXTENSION_TYPES.get(f".{extension.lower()}", DocType.UNKNOWN)
    return DocType.HTML if scheme in WEB_SCHEMES else DocType.UNKNOWN


def _page_and_section(fragment: str) -> tuple[int | None, str | None]:
    """Extract the requested page/section from a URL ``fragment``.

    A ``page=N`` token yields the page; any other non-empty fragment becomes the section.
    """
    if not fragment:
        return None, None
    match = _PAGE_FRAGMENT.search(fragment)
    if match:
        return int(match.group(1)), None
    return None, fragment
