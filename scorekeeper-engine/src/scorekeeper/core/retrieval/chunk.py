"""Chunking a retrieved document's sentences for embedding.

:func:`chunk_sentences` groups the sentences the extract stage produced into
**overlapping** windows — ``size`` sentences per chunk, ``overlap`` shared with the
previous one. Overlap is what keeps a claim that straddles a chunk boundary readable in
at least one chunk. Pure, no I/O.

A window carries where it came from, not just its text: the extract stage attributes every
sentence to a page, and grouping them would throw that away. See :class:`SentenceWindow`.

A sentence the extract stage marked ``atomic`` — a rendered table — is a hard boundary and
becomes a window of its own, because half a grid grounds nothing.

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

    An :attr:`~scorekeeper.core.retrieved_context.Sentence.atomic` sentence is a hard
    boundary: it becomes a window of its own and never shares one with a neighbour. A
    rendered table is the case — half a table grounds nothing, and the overlap that
    rescues a straddling claim does not apply to a grid. With no atomic sentence the
    output is exactly the plain sliding window over the whole sequence.

    Raises ``ValueError`` when ``size < 1`` or ``overlap`` is negative or not smaller
    than ``size`` — an overlap that consumes the whole window would never advance.
    """
    if size < 1:
        raise ValueError(f"El tamaño del chunk debe ser al menos 1 (recibido {size})")
    if not 0 <= overlap < size:
        raise ValueError(
            f"El solape debe estar entre 0 y {size - 1} (recibido {overlap})"
        )
    windows: list[SentenceWindow] = []
    run: "list[Sentence]" = []  # the non-atomic sentences awaiting a sliding window
    for sentence in sentences:
        if not sentence.atomic:
            run.append(sentence)
            continue
        windows.extend(_sliding_windows(run, size=size, overlap=overlap))
        run = []
        windows.append(_window([sentence]))
    windows.extend(_sliding_windows(run, size=size, overlap=overlap))
    return windows


def _sliding_windows(
    sentences: "Sequence[Sentence]", *, size: int, overlap: int
) -> list[SentenceWindow]:
    """Overlapping windows over a run of sentences, none of them atomic."""
    step = size - overlap
    windows: list[SentenceWindow] = []
    for start in range(0, len(sentences), step):
        # A short tail already covered by the previous window adds nothing.
        if start and start + overlap >= len(sentences):
            break
        windows.append(_window(sentences[start : start + size]))
    return windows


def _window(sentences: "Sequence[Sentence]") -> SentenceWindow:
    """One window over ``sentences``, carrying where its head came from."""
    return SentenceWindow(
        text=" ".join(s.text for s in sentences),
        # The first sentence's page, deliberately: see ``SentenceWindow``.
        page=sentences[0].page,
        sentence_start=sentences[0].index,
        sentence_end=sentences[-1].index,
    )
