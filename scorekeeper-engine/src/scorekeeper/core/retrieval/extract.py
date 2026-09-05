"""Concrete Extract stage: turn a fetched document into markdown.

``MarkdownContentExtractor`` implements the
:class:`~scorekeeper.core.retrieval.protocols.ContentExtractor` contract — ``supports(doc_type)``
and ``extract(document, locator) -> ExtractedContent`` — the fifth concrete stage of the
retrieval pipeline (see ``docs/retrieval-pipeline.md``), between fetch and assemble. It also
performs the pipeline's *filter*: there is no separate filter stage; selecting the requested
page happens here, inside ``extract``.

Per document type:

* **PDF** — page by page. With ``locator.page`` (from a ``#page=N`` fragment) only that page
  is sliced out with ``pypdf`` and converted; with no page **every** page is sliced and
  converted in turn, so each sentence keeps the page it came from. That costs one conversion
  per page instead of one for the whole file — the price of page attribution, since MarkItDown
  emits no page boundaries — and the concatenated markdown is not byte-identical to converting
  the file in one pass (a table straddling a page break is split).
* **DOCX** (Word ``.docx``) — the whole document is converted; ``.docx`` stores no page
  boundaries, so the page selector is ignored. Legacy binary ``.doc`` is not supported.
* **HTML** — the fetched page bytes are converted.

Conversion goes through **MarkItDown** (imported lazily; ships in the optional ``retrieval``
extra), preserving titles, lists, and tables. **Images/graphics and links are dropped**:
MarkItDown keeps both as markdown, so this stage strips image markup from the result and
strips links out of it entirely — anchor text included, along with a URL that is only a URL
(autolink or bare).

Every converted document is then segmented into :class:`~scorekeeper.core.retrieved_context.Sentence`
with ``syntok`` (``core.text.split_sentences``, the same segmenter ``faithfulness`` uses on the
answer). The sentences carry their page and a document-wide 0-based index; they are working
material for a later chunker, not judge input — ``ExtractedContent.text`` remains what a judge
reads, so the assemble stage's blank-markdown rule is unchanged.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import TYPE_CHECKING

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.types import (
    DocType,
    DocumentLocator,
    ExtractedContent,
    FetchedDocument,
    Sentence,
)
from scorekeeper.core.text import split_sentences

if TYPE_CHECKING:
    from collections.abc import Callable

    from pypdf import PdfReader

    from scorekeeper.core.retrieval.protocols import ContentExtractor

logger = logging.getLogger(__name__)

# Document types this extractor can convert to markdown.
_SUPPORTED = frozenset({DocType.PDF, DocType.DOCX, DocType.HTML})

# MarkItDown ``file_extension`` hint per document type.
_EXTENSIONS = {DocType.PDF: ".pdf", DocType.DOCX: ".docx", DocType.HTML: ".html"}

# Image markup to remove (inline / reference markdown images and raw <img> tags).
_IMAGE_INLINE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMAGE_REFERENCE = re.compile(r"!\[[^\]]*\]\[[^\]]*\]")
_IMAGE_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

# Link markup to remove: a link goes entirely — anchor text and target alike — and so does a
# bare or autolinked URL.
_LINK_INLINE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_LINK_REFERENCE = re.compile(r"\[[^\]]*\]\[[^\]]*\]")
_LINK_DEFINITION = re.compile(r"^[ \t]*\[[^\]]*\]:[ \t]*\S+.*$", re.MULTILINE)
_LINK_TAG = re.compile(r"<a\b[^>]*>.*?</a>|</?a\b[^>]*>", re.IGNORECASE | re.DOTALL)
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^>\s]*>", re.IGNORECASE)
_BARE_URL = re.compile(
    r"\(?(?:https?://|www\.)[^\s<>)\]]*[^\s<>)\].,;:!?\"']\)?", re.IGNORECASE
)

# Collapsers for the blank-line / space runs the removals above can leave behind. The
# space run is any horizontal whitespace but a newline — tabs and the non-breaking /
# unicode spaces an HTML conversion emits (``&nbsp;``) included — so two words never stay
# separated by more than one space.
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"[^\S\n\r\f\v]{2,}")


class ExtractError(Exception):
    """Raised when a document cannot be converted to markdown (corrupt / conversion failure)."""


class PageNotFoundError(ExtractError):
    """Raised when the requested page is absent — a future orchestrator maps it to
    ``RetrievalStatus.LOCATOR_NOT_FOUND``."""


def default_content_extractor() -> "ContentExtractor":
    """The extract stage ``settings.retrieval_extractor`` selects.

    ``"unstructured"`` needs ``unstructured_api_url`` to post to; without it — or for any
    unrecognised value — the markdown extractor is returned and a warning logged. A typo in
    ``.env`` must degrade the extraction, not take every retrieval down.
    """
    settings = get_settings()
    if settings.retrieval_extractor == "markdown":
        return MarkdownContentExtractor()
    if settings.retrieval_extractor == "unstructured":
        if settings.unstructured_api_url:
            # Imported here so the markdown path never loads the HTTP extractor.
            from scorekeeper.core.retrieval.extract_unstructured import (
                UnstructuredContentExtractor,
            )

            return UnstructuredContentExtractor()
        logger.warning(
            "retrieval_extractor=unstructured pero falta unstructured_api_url; "
            "se usa el extractor markdown"
        )
        return MarkdownContentExtractor()
    logger.warning(
        "Extractor desconocido %r; se usa el extractor markdown",
        settings.retrieval_extractor,
    )
    return MarkdownContentExtractor()


def read_pdf(body: bytes) -> "PdfReader":
    """Parse ``body`` as a PDF, raising :class:`ExtractError` when it is malformed.

    Shared with the unstructured extractor: both need the page count before deciding
    what to convert, and both report a corrupt file the same way.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(body))
        len(reader.pages)  # the page tree is parsed lazily; force it here
    except Exception as exc:  # malformed PDF
        raise ExtractError(f"No se pudo leer el PDF: {exc}") from exc
    return reader


def check_page(page: int, total: int) -> None:
    """Raise :class:`PageNotFoundError` when ``page`` (1-based) is outside the document."""
    if not 1 <= page <= total:
        raise PageNotFoundError(
            f"La página {page} no existe (el documento tiene {total} página(s))"
        )


def slice_page(reader: "PdfReader", page: int) -> bytes:
    """The one-page PDF holding ``page`` (1-based) of ``reader``.

    Slicing is what attributes extracted text to a page: neither MarkItDown nor a
    whole-file partition tells us which page a line came from.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(reader.pages[page - 1])
    sliced = BytesIO()
    writer.write(sliced)
    return sliced.getvalue()


class MarkdownContentExtractor:
    """Convert a ``FetchedDocument`` to markdown, selecting the requested PDF page.

    ``converter`` is the seam to the markdown backend: a ``(body, file_extension) -> markdown``
    callable, injectable for tests. When ``None`` a default is built lazily around a cached
    ``markitdown.MarkItDown`` instance, so the taxonomy stays importable without the extra.
    """

    def __init__(self, *, converter: Callable[[bytes, str], str] | None = None) -> None:
        self._converter = converter
        self._markitdown = None  # lazily-built default MarkItDown instance

    # -- ContentExtractor protocol ------------------------------------------------------

    def supports(self, doc_type: DocType) -> bool:
        """Whether this extractor handles ``doc_type`` (PDF, DOCX, HTML)."""
        return doc_type in _SUPPORTED

    def extract(
        self, document: FetchedDocument, locator: DocumentLocator
    ) -> ExtractedContent:
        """Convert ``document`` to markdown per page and segment it into sentences.

        A PDF yields one markdown block per page (only ``locator.page`` when set, else every
        page); DOCX/HTML yield a single pageless block. The blocks are joined into ``text``
        and segmented, in order, into ``sentences``.
        """
        if document.doc_type is DocType.PDF:
            pages = self._extract_pdf(document.body, locator.page)
        elif document.doc_type in (DocType.DOCX, DocType.HTML):
            pages = [(None, self._convert(document.body, _EXTENSIONS[document.doc_type]))]
        else:
            raise ExtractError(f"Tipo de documento no soportado: {document.doc_type}")

        # Strip images and links before segmenting so no sentence is built out of markup.
        blocks = [(page, strip_markup(markdown).strip()) for page, markdown in pages]
        sentences: list[Sentence] = []
        for page, block in blocks:
            for sentence in split_sentences(block):
                sentences.append(Sentence(page=page, index=len(sentences), text=sentence))
        text = "\n\n".join(block for _, block in blocks if block)
        return ExtractedContent(text=text, page=locator.page, sentences=sentences)

    # -- helpers ------------------------------------------------------------------------

    def _extract_pdf(self, body: bytes, page: int | None) -> list[tuple[int, str]]:
        """Convert ``page`` (1-based), or every page, as ``(page number, markdown)`` pairs.

        Each page is sliced into its own one-page document with ``pypdf`` before conversion,
        which is what attributes a sentence to a page. A PDF with no pages yields ``[]``.
        """
        reader = read_pdf(body)
        total = len(reader.pages)
        if page is not None:
            check_page(page, total)
        numbers = [page] if page is not None else range(1, total + 1)
        return [
            (number, self._convert(slice_page(reader, number), ".pdf"))
            for number in numbers
        ]

    def _convert(self, body: bytes, file_extension: str) -> str:
        """Convert ``body`` to markdown via the injected converter or MarkItDown."""
        if self._converter is not None:
            return self._converter(body, file_extension)
        if self._markitdown is None:
            from markitdown import MarkItDown

            self._markitdown = MarkItDown()
        try:
            result = self._markitdown.convert_stream(
                BytesIO(body), file_extension=file_extension
            )
        except Exception as exc:  # markitdown conversion failure
            raise ExtractError(
                f"No se pudo convertir el documento ({file_extension}): {exc}"
            ) from exc
        return result.text_content


def strip_markup(markdown: str) -> str:
    """Remove image and link markup from ``markdown``.

    Images (markdown and raw ``<img>``) go entirely, and so does every link — markdown links,
    reference links and their definitions, ``<a>`` elements — anchor text included, along with
    a URL that is only a URL (autolink or bare).
    """
    markdown = _IMAGE_INLINE.sub("", markdown)
    markdown = _IMAGE_REFERENCE.sub("", markdown)
    markdown = _IMAGE_TAG.sub("", markdown)
    markdown = _LINK_INLINE.sub("", markdown)
    markdown = _LINK_REFERENCE.sub("", markdown)
    markdown = _LINK_DEFINITION.sub("", markdown)
    markdown = _LINK_TAG.sub("", markdown)
    markdown = _AUTOLINK.sub("", markdown)
    markdown = _BARE_URL.sub("", markdown)
    return _BLANK_RUN.sub("\n\n", _SPACE_RUN.sub(" ", markdown))
