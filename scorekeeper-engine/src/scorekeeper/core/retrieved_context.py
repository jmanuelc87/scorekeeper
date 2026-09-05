"""Structured retrieved context for a turn.

A turn's ``retrieved_context`` is the set of documents a RAG answer was grounded on,
assembled from the ``retrieved_documents`` rows of that turn::

    {"documents": [{"name": ..., "document": ..., "url": ..., "chunks": [...]}, ...]}

The array is **ordered by retriever rank** (rank 1 first) — ``contextual_precision``
relies on that order, which is the *platform's* retriever order and never ours.

A document carries no whole-document text. Its text arrives as ``chunks``: the
overlapping sentence windows the embedding phase stored, already narrowed to the ones
most similar to the turn's response (``db.repositories.embeddings``). The
``sentences`` field is the other direction — what the extract stage produced on the way
*in*, and the chunker's input; it is left empty when a document is read back for scoring.

Groundedness metrics (``hallucination``, ``contextual_precision``) evaluate each
document's *node text* — its ``document`` plus its selected chunks — via
:meth:`node_texts`. Judges render the whole context to Spanish text via
:meth:`RetrievedContext.render`.
"""

from __future__ import annotations

from pydantic import BaseModel


class Sentence(BaseModel):
    """One sentence segmented out of a retrieved document.

    The chunker's input, not judge input: what a judge reads is the selected
    :class:`Chunk` list. ``page`` is the 1-based PDF page the sentence came from
    (``None`` for DOCX/HTML, which carry no page boundaries), and ``index`` orders
    the sentence within its whole document (0-based, never restarting per page).

    ``atomic`` marks a sentence the chunker must not merge with its neighbours — a
    rendered table, which reads as one unit or not at all. Rows written before the
    flag existed simply lack the key and read back as ``False``.
    """

    page: int | None = None
    index: int
    text: str
    atomic: bool = False


class Chunk(BaseModel):
    """One stored, embedded window of a document, as handed to a judge.

    The embedding itself never leaves the database: the ranking runs in SQL
    (``db.repositories.embeddings``), so the vectors never enter the ``TurnView``
    that ``metrics.fingerprint`` hashes whole.
    """

    index: int  # Order of the chunk within its document, 0-based.
    text: str


class RetrievedDocument(BaseModel):
    """One retrieved document a RAG answer may have been grounded on."""

    name: str  # Short label/title for the retrieved item.
    document: str  # Source document reference (filename, title, id).
    url: str | None = None  # Source URL, for retrieved web documents.
    # Write path (extract -> database): the document's sentences, page-attributed.
    sentences: list[Sentence] = []
    # Read path (database -> judge): the chunks selected for this turn, in document order.
    chunks: list[Chunk] = []

    @property
    def content(self) -> str:
        """The selected chunks as one block of text."""
        return "\n".join(chunk.text for chunk in self.chunks)

    def node_text(self) -> str:
        """Text a groundedness judge evaluates for this document.

        The premise/node is the source ``document`` followed by its selected chunks.
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
