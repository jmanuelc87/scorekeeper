"""Chunking a retrieved document's sentences for embedding.

:func:`chunk_sentences` groups the sentences the extract stage produced into
**overlapping** windows — ``size`` sentences per chunk, ``overlap`` shared with the
previous one. Overlap is what keeps a claim that straddles a chunk boundary readable in
at least one chunk. Pure, no I/O.

A window carries where it came from, not just its text: the extract stage attributes every
sentence to a page, and grouping them would throw that away. See :class:`SentenceWindow`.

The other half of the pair — picking the stored chunks closest to a query embedding —
happens in SQL, in ``db.repositories.embeddings``, so the vectors never leave the
database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scorekeeper.core.retrieved_context import Sentence


class SentenceWindow(BaseModel):
    """One window of sentences: the chunk's text and where in the document it came from.

    ``page`` is the page of the window's **first** sentence — a window spanning ``size``
    sentences can straddle a page break, and only one page is recorded, so the tail of
    such a window is attributed to the page its head came from. ``None`` for DOCX/HTML,
    which have no pages.

    ``sentence_start``/``sentence_end`` are ``Sentence.index`` values, inclusive both
    ends, so a stored chunk stays traceable back to ``RetrievedContextDocument.sentences``.
    """

    text: str
    page: int | None = None
    sentence_start: int
    sentence_end: int


def chunk_sentences(
    sentences: "Sequence[Sentence]", *, size: int, overlap: int
) -> list[SentenceWindow]:
    """Group ``sentences`` into overlapping windows of ``size``, each joined into one text.

    Advances by ``size - overlap`` sentences per chunk, so consecutive chunks share
    ``overlap`` sentences. The last window may be short. No sentences yields ``[]``.

    Raises ``ValueError`` when ``size < 1`` or ``overlap`` is negative or not smaller
    than ``size`` — an overlap that consumes the whole window would never advance.
    """
    if size < 1:
        raise ValueError(f"El tamaño del chunk debe ser al menos 1 (recibido {size})")
    if not 0 <= overlap < size:
        raise ValueError(
            f"El solape debe estar entre 0 y {size - 1} (recibido {overlap})"
        )
    step = size - overlap
    windows: list[SentenceWindow] = []
    for start in range(0, len(sentences), step):
        # A short tail already covered by the previous window adds nothing.
        if start and start + overlap >= len(sentences):
            break
        window = sentences[start : start + size]
        windows.append(
            SentenceWindow(
                text=" ".join(s.text for s in window),
                # The first sentence's page, deliberately: see ``SentenceWindow``.
                page=window[0].page,
                sentence_start=window[0].index,
                sentence_end=window[-1].index,
            )
        )
    return windows
