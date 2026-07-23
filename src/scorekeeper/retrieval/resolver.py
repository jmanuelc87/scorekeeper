"""Concrete Locate stage: resolve a ``SourceRef`` into a concrete fetch target.

``UrlDocumentLocatorResolver`` implements the
:class:`~scorekeeper.retrieval.protocols.DocumentLocatorResolver` contract —
``resolve(source) -> DocumentLocator``. It is the second concrete stage of the retrieval
pipeline (see ``docs/retrieval-pipeline.md``), sitting between parse and authorize; the
later stages (authorize/fetch/extract) remain deferred.

Resolution is **pure and deterministic**: ``urllib.parse`` only, no network, no PDF/HTML
parsing, no LLM, and no credentials. From a ``SourceRef``'s raw URL it derives the fetch
key (URL minus fragment), the filename, the :class:`DocType`, the host, and the
page/section requested by a ``#page=N`` fragment.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urldefrag, urlsplit

from scorekeeper.retrieval.types import DocType, DocumentLocator, SourceRef

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
        """Derive the document URL, filename, type, host, and page/section from ``source``.

        The fragment is stripped for ``document_url`` (the fetch/cache key) but mined for
        the requested ``page``/``section``; the query string is preserved in the URL and
        ignored for the filename.
        """
        document_url, fragment = urldefrag(source.url)
        parts = urlsplit(source.url)
        filename = _filename_from_path(parts.path)
        page, section = _page_and_section(fragment)
        return DocumentLocator(
            document_url=document_url,
            filename=filename,
            doc_type=_doc_type_for(filename),
            host=parts.netloc.lower(),
            page=page,
            section=section,
        )


def _filename_from_path(path: str) -> str:
    """Return the URL-unquoted final path segment (``""`` when there is none)."""
    last_segment = path.rsplit("/", 1)[-1]
    return unquote(last_segment)


def _doc_type_for(filename: str) -> DocType:
    """Classify ``filename`` by its lowercased extension; ``UNKNOWN`` if unrecognized."""
    _, _, extension = filename.rpartition(".")
    if not extension or extension == filename:
        return DocType.UNKNOWN
    return _EXTENSION_TYPES.get(f".{extension.lower()}", DocType.UNKNOWN)


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
