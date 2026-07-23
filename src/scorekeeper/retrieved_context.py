"""Structured retrieved context for a turn.

A turn's ``retrieved_context`` is the set of documents a RAG answer was grounded
on. It is stored on the ``Turn`` row as a JSON object with the canonical shape::

    {"documents": [{"name": ..., "document": ..., "content": ..., "url": ...}, ...]}

The array is **ordered by retriever rank** (rank 1 first) — ``contextual_precision``
relies on that order. Each ``RetrievedDocument`` carries a ``name`` (label/title), a
``document`` (source reference such as a filename or title), the ``content`` (the
retrieved text), and an optional ``url`` for web documents.

Groundedness metrics (``hallucination``, ``contextual_precision``) evaluate each
document's *node text* — its ``document`` plus ``content`` — via :meth:`node_texts`,
which replaces the old ``split_context_docs`` blank-line splitter. Judges render the
whole context to Spanish text via :meth:`RetrievedContext.render`.

``from_blob`` keeps legacy plain-text spreadsheet cells importing losslessly by
blank-line-splitting them into content-only documents.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

# Legacy blank-line document delimiter, kept for plain-text ``.xlsx`` cells.
_BLANK_LINE = re.compile(r"\n\s*\n")


class RetrievedDocument(BaseModel):
    """One retrieved document a RAG answer may have been grounded on."""

    name: str  # Short label/title for the retrieved item.
    document: str  # Source document reference (filename, title, id).
    content: str  # The retrieved text.
    url: str | None = None  # Source URL, for retrieved web documents.

    def node_text(self) -> str:
        """Text a groundedness judge evaluates for this document.

        The premise/node is the source ``document`` followed by its ``content``.
        """
        return f"{self.document}\n{self.content}".strip()


class RetrievedContext(BaseModel):
    """Ordered set of documents retrieved for a turn (rank 1 first)."""

    documents: list[RetrievedDocument] = []

    @property
    def is_empty(self) -> bool:
        return not self.documents

    def node_texts(self) -> list[str]:
        """Ordered per-document node text for groundedness metrics.

        Replaces the old ``split_context_docs``: documents keep their retriever
        rank order and empty nodes are dropped.
        """
        return [text for doc in self.documents if (text := doc.node_text())]

    def render(self) -> str:
        """Render the context as a Spanish text block for judge prompts.

        Returns ``""`` when there is no context so callers can skip the section.
        Each document is rendered with its label, source (and URL when present)
        followed by its content; documents are separated by blank lines.
        """
        blocks: list[str] = []
        for doc in self.documents:
            source = doc.document
            if doc.url:
                source = f"{source} — {doc.url}" if source else doc.url
            header = doc.name
            if source:
                header = f"{header} ({source})" if header else source
            block = f"{header}\n{doc.content}".strip() if header else doc.content.strip()
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    @classmethod
    def from_blob(cls, text: str | None) -> "RetrievedContext":
        """Build a context from a legacy plain-text cell.

        Blank-line-separated blocks become content-only documents (``name`` and
        ``document`` empty, ``url`` ``None``), preserving the old splitting
        behaviour for spreadsheets that predate the JSON schema.
        """
        blocks = [block.strip() for block in _BLANK_LINE.split(text or "")]
        return cls(
            documents=[
                RetrievedDocument(name="", document="", content=block)
                for block in blocks
                if block
            ]
        )
