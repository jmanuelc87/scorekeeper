"""Concrete Extract stage backed by the ``unstructured-api`` service.

``UnstructuredContentExtractor`` implements the same
:class:`~scorekeeper.core.retrieval.protocols.ContentExtractor` contract as
:class:`~scorekeeper.core.retrieval.extract.MarkdownContentExtractor` — ``supports(doc_type)``
and ``extract(document, locator) -> ExtractedContent`` — and is selected in its place by
``settings.retrieval_extractor`` (see ``docs/retrieval-pipeline.md``). Both ship; the setting
picks one, so a regression is a configuration change rather than a revert.

**It calls a service, not a library.** The document bytes are POSTed to ``unstructured-api``
(``compose.yaml``), which answers with typed elements. The heavy half of that work — the layout
model, OCR, poppler, tesseract — lives in that image, so the engine gains no dependency beyond
the ``httpx2`` it already has. The call is blocking, which is fine: the orchestrator already runs
``extract`` in ``asyncio.to_thread``.

What it buys over the markdown extractor:

* **Tables survive.** A ``Table`` element carries ``metadata.text_as_html``, rendered here into a
  pipe table and emitted as a single ``atomic`` sentence so the chunker never splits a grid.
  Each table is then **described** by Claude Haiku 4.5 (see
  :mod:`~scorekeeper.core.retrieval.describe`) and the description stored as the line above the
  grid, so the chunk a judge reads carries the table's subject and not only its cells.
* **Pages come from the document.** Each element reports ``metadata.page_number``, so a PDF
  referenced without ``#page=N`` is converted in **one** request instead of one per page, and a
  table straddling a page break is no longer cut at the boundary.
* **Scanned pages are readable.** A page whose text layer comes back empty is re-posted with
  ``strategy="hi_res"``, which runs OCR. Best-effort: a failure leaves that page empty rather
  than failing the document.

Structural noise is dropped by **category** — ``Header``, ``Footer``, ``PageNumber``, ``Image``
and friends — rather than by regex, so running heads and page numbers stop riding into every
chunk. Links and bare URLs are still stripped with
:func:`~scorekeeper.core.retrieval.extract.strip_markup`, which remains load-bearing: the API
returns prose, and prose contains bare URLs.

**One deliberate divergence from the markdown extractor.** ``MarkdownContentExtractor`` removes an
``<a href=…>`` element whole, anchor text included. Here the anchor text arrives as ordinary
narrative and survives. Anchor text is content rather than plumbing, so this is an improvement —
but it does mean the two extractors do not produce identical text for an HTML document.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from scorekeeper.config.settings import get_settings
from scorekeeper.core.retrieval.describe import TableDescriber
from scorekeeper.core.retrieval.extract import (
    ExtractError,
    check_page,
    read_pdf,
    slice_page,
    strip_markup,
)
from scorekeeper.core.retrieval.types import (
    DocType,
    DocumentLocator,
    ExtractedContent,
    FetchedDocument,
    Sentence,
)
from scorekeeper.core.text import split_sentences

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pypdf import PdfReader

logger = logging.getLogger(__name__)

# Document types this extractor handles — the same three the markdown extractor does.
_SUPPORTED = frozenset({DocType.PDF, DocType.DOCX, DocType.HTML})

# Filename and media type sent with the upload; the API dispatches on them.
_FILENAMES = {
    DocType.PDF: "documento.pdf",
    DocType.DOCX: "documento.docx",
    DocType.HTML: "documento.html",
}
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".html": "text/html",
}

# Structural furniture: page decoration and graphics, dropped whole. FigureCaption goes with
# the figure it describes, matching the markdown extractor's "images and their alt text go".
_DROPPED = frozenset(
    {"Header", "Footer", "PageNumber", "PageBreak", "Image", "Figure", "FigureCaption"}
)
# Rendered as a heading. Flat ``##`` for all of them on purpose: ``category_depth`` differs
# between the fast and hi_res strategies, and the OCR fallback crosses exactly that boundary,
# so honouring it would make one document's headings depend on which pages were scanned.
_HEADINGS = frozenset({"Title", "SectionHeader", "Headline", "Subtitle"})
_TABLE = "Table"
_LIST_ITEM = "ListItem"

# Tags that separate words inside a table cell. Without them the text nodes either side of
# a ``<br>`` would be concatenated ("a<br>b" -> "ab"); inline formatting (``<b>``, ``<sup>``)
# is deliberately absent, so "1<sup>er</sup>" still reads "1er".
_CELL_BREAKS = frozenset({"br", "p", "div", "li", "tr"})

# The strategy the OCR fallback escalates to.
_HI_RES = "hi_res"


class Element(BaseModel):
    """One element as the partitioner reports it — the seam's currency.

    A neutral value object rather than an ``unstructured`` type, so a test can build a
    document's elements as a literal list and the suite never needs the service (nor the
    library) to run. ``html`` is ``metadata.text_as_html``, present for a ``Table`` and
    usually absent under the ``fast`` strategy, which infers no table structure.
    """

    category: str  # the API's element "type": "Title", "NarrativeText", "Table", …
    text: str = ""
    page: int | None = None  # metadata.page_number
    html: str | None = None  # metadata.text_as_html


# ``(body, *, filename, strategy) -> elements``. ``strategy`` is on the seam deliberately: the
# OCR fallback is observable only as a second call carrying ``hi_res``.
Partitioner = Callable[..., list[Element]]


class UnstructuredContentExtractor:
    """Convert a ``FetchedDocument`` to markdown through ``unstructured-api``.

    ``partitioner`` is the seam to the service: a ``(body, *, filename, strategy) -> [Element]``
    callable, injectable for tests. When ``None`` a default HTTP client is built lazily on first
    use, so importing this module needs neither the service nor its URL.

    ``describer`` is the seam to the table describer, a ``table -> description | None``
    callable, built lazily on the first table for the same reason — so importing this module
    needs neither the Agent SDK nor Claude credentials.
    """

    def __init__(
        self,
        *,
        partitioner: "Partitioner | None" = None,
        describer: TableDescriber | None = None,
    ) -> None:
        self._partitioner = partitioner
        self._describer = describer

    # -- ContentExtractor protocol ------------------------------------------------------

    def supports(self, doc_type: DocType) -> bool:
        """Whether this extractor handles ``doc_type`` (PDF, DOCX, HTML)."""
        return doc_type in _SUPPORTED

    def extract(
        self, document: FetchedDocument, locator: DocumentLocator
    ) -> ExtractedContent:
        """Partition ``document``, render it to markdown and segment it into sentences.

        A PDF is partitioned per ``locator.page`` when the reference carries one and whole
        otherwise; DOCX and HTML are partitioned whole and carry no page. Tables become single
        ``atomic`` sentences; everything else is segmented with syntok, the index running
        across the whole document.
        """
        settings = get_settings()
        if document.doc_type is DocType.PDF:
            elements = self._pdf_elements(document.body, locator, settings)
        elif document.doc_type in (DocType.DOCX, DocType.HTML):
            elements = self._partition(
                document.body, document.doc_type, settings.unstructured_strategy
            )
            # Neither format carries page boundaries; whatever the API reports is discarded
            # so a sentence's page means the same thing for every document type.
            for element in elements:
                element.page = None
        else:
            raise ExtractError(f"Tipo de documento no soportado: {document.doc_type}")

        blocks = _blocks(elements)
        sentences: list[Sentence] = []
        rendered: list[str] = []
        described = 0
        for page, markdown, is_table in blocks:
            if is_table:
                # Described once per table, then repeated on every piece: each piece is a
                # standalone table a judge may read alone, and the description is what tells
                # it what the grid is about. Prepending after the split, not before, because
                # ``_split_table`` reads the first two lines as header and separator.
                description = self._describe(markdown)
                described += description is not None
                # Every piece carries the description, so the rows it may hold shrink by
                # exactly that much: ``extract_table_max_chars`` bounds one embedding
                # input, and the description is part of it.
                budget = settings.extract_table_max_chars
                if budget > 0 and description:
                    budget = max(1, budget - len(_with_description("", description)))
                texts = [
                    _with_description(piece, description)
                    for piece in _split_table(
                        markdown, budget, settings.extract_table_overlap_rows
                    )
                ]
                rendered.append(_with_description(markdown, description))
            else:
                texts = split_sentences(markdown)
                rendered.append(markdown)
            for text in texts:
                sentences.append(
                    Sentence(
                        page=page, index=len(sentences), text=text, atomic=is_table
                    )
                )
        text = "\n\n".join(rendered)
        logger.debug(
            "extract: %s página=%s -> %d elemento(s), %d bloque(s) (%d tabla(s), "
            "%d descrita(s)), %d oración(es), %d carácter(es)",
            document.doc_type.value,
            locator.page,
            len(elements),
            len(blocks),
            sum(1 for _, _, is_table in blocks if is_table),
            described,
            len(sentences),
            len(text),
        )
        return ExtractedContent(text=text, page=locator.page, sentences=sentences)

    # -- helpers ------------------------------------------------------------------------

    def _describe(self, table: str) -> str | None:
        """``table``'s description, building the default describer on first use.

        Best-effort like the describer itself: a table that cannot be described keeps the
        text it already had.
        """
        if self._describer is None:
            from scorekeeper.core.retrieval.describe import AgentTableDescriber

            self._describer = AgentTableDescriber()
        return self._describer(table)

    def _pdf_elements(
        self, body: bytes, locator: DocumentLocator, settings: Any
    ) -> list[Element]:
        """The PDF's elements, page-attributed, with text-less pages escalated to OCR.

        With ``locator.page`` the requested page is sliced out first — posting a 300-page
        document to keep one page is indefensible, and it is the slice that lets pypdf raise
        ``PageNotFoundError`` before the service is ever called. Without a fragment the whole
        file goes in one request and the pages come back on the elements.
        """
        reader = read_pdf(body)
        total = len(reader.pages)
        if locator.page is not None:
            check_page(locator.page, total)
            elements = self._partition(
                slice_page(reader, locator.page),
                DocType.PDF,
                settings.unstructured_strategy,
            )
            # A one-page slice is reported as page 1 whatever page it really was; without
            # this every chunk of every ``#page=N`` document would be filed under page 1.
            for element in elements:
                element.page = locator.page
            pages = [locator.page]
            logger.debug(
                "extract: página %d de %d recortada antes de partitionar", locator.page, total
            )
        else:
            elements = self._partition(body, DocType.PDF, settings.unstructured_strategy)
            _carry_pages(elements)
            pages = list(range(1, total + 1))
            logger.debug("extract: PDF de %d página(s) partitionado entero", total)

        if (
            settings.unstructured_ocr_fallback
            and settings.unstructured_strategy != _HI_RES
        ):
            elements = self._ocr_empty_pages(elements, reader, pages, settings)
        return elements

    def _ocr_empty_pages(
        self,
        elements: list[Element],
        reader: "PdfReader",
        pages: "Sequence[int]",
        settings: Any,
    ) -> list[Element]:
        """Re-partition every page the text layer left empty with ``hi_res``.

        Only empty pages are re-posted, so a mostly-digital document with a scanned insert
        pays one extra request per insert rather than one per page. Wholly best-effort: past
        ``unstructured_max_ocr_pages`` the escalation is skipped, and a failed OCR request
        leaves its page empty instead of failing the document.
        """
        with_text = {
            element.page
            for element in elements
            if element.page is not None and element.text.strip()
        }
        empty = [page for page in pages if page not in with_text]
        logger.debug(
            "extract: %d de %d página(s) sin texto tras la estrategia base", len(empty), len(pages)
        )
        if not empty:
            return elements
        if len(empty) > settings.unstructured_max_ocr_pages:
            logger.warning(
                "Se omite el OCR: %d página(s) sin texto superan el máximo de %d",
                len(empty),
                settings.unstructured_max_ocr_pages,
            )
            return elements

        recovered: dict[int, list[Element]] = {}
        for page in empty:
            try:
                ocr = self._partition(slice_page(reader, page), DocType.PDF, _HI_RES)
            except ExtractError:
                logger.warning(
                    "No se pudo aplicar OCR a la página %d; queda sin texto",
                    page,
                    exc_info=True,
                )
                continue
            for element in ocr:
                element.page = page
            recovered[page] = ocr
        logger.debug(
            "extract: OCR recuperó %d de %d página(s) vacía(s)", len(recovered), len(empty)
        )
        if not recovered:
            return elements

        # Rebuild in page order, keeping each page's own element order. For a PDF that is
        # the document order the partitioner already returned, so nothing is reshuffled.
        merged: list[Element] = []
        for page in pages:
            merged.extend(
                recovered.get(page)
                or [element for element in elements if element.page == page]
            )
        merged.extend(element for element in elements if element.page is None)
        return merged

    def _partition(
        self, body: bytes, doc_type: DocType, strategy: str
    ) -> list[Element]:
        """Call the seam, building the default HTTP partitioner on first use."""
        if self._partitioner is None:
            self._partitioner = _ApiPartitioner()
        logger.debug(
            "extract: partitionando %d byte(s) como %s con strategy=%s",
            len(body),
            _FILENAMES[doc_type],
            strategy,
        )
        elements = self._partitioner(
            body, filename=_FILENAMES[doc_type], strategy=strategy
        )
        logger.debug(
            "extract: %d elemento(s) devuelto(s); categorías: %s",
            len(elements),
            ", ".join(sorted({element.category for element in elements})) or "ninguna",
        )
        return elements


class _ApiPartitioner:
    """The default seam: POST the document to ``unstructured-api`` and map its JSON back.

    One ``httpx2.Client`` is kept for connection reuse. Every failure — connection, timeout,
    HTTP status, unparseable body — surfaces as :class:`ExtractError`, which the orchestrator
    records against the one reference and moves on.
    """

    def __init__(self, *, client: Any = None) -> None:
        settings = get_settings()
        base = (settings.unstructured_api_url or "").rstrip("/")
        self._url = f"{base}/general/v0/general"
        self._api_key = settings.unstructured_api_key
        self._languages = [
            language.strip()
            for language in settings.unstructured_ocr_languages.split(",")
            if language.strip()
        ]
        self._timeout = settings.unstructured_timeout_seconds
        self._client = client

    def __call__(
        self, body: bytes, *, filename: str, strategy: str
    ) -> list[Element]:
        import httpx2

        if self._client is None:
            self._client = httpx2.Client(timeout=self._timeout)
        extension = filename[filename.rfind(".") :]
        # A list value is how a multipart form repeats a field; a list of ``(key, value)``
        # pairs is not accepted alongside ``files``.
        form: dict[str, Any] = {
            "strategy": strategy,
            # Ask for table structure; it is what makes a Table element worth having.
            "pdf_infer_table_structure": "true",
            # Coordinates would be a large payload we never read.
            "coordinates": "false",
        }
        if self._languages:
            form["languages"] = self._languages
        headers = {"unstructured-api-key": self._api_key} if self._api_key else {}
        try:
            response = self._client.post(
                self._url,
                files={"files": (filename, body, _CONTENT_TYPES[extension])},
                data=form,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # connection, timeout, status, or a non-JSON body
            raise ExtractError(
                f"No se pudo convertir el documento en unstructured-api: {exc}"
            ) from exc
        if not isinstance(payload, list):
            raise ExtractError(
                "Respuesta inesperada de unstructured-api: se esperaba una lista de elementos"
            )
        return [_element(entry) for entry in payload]


def _element(entry: Any) -> Element:
    """One API element as an :class:`Element`, tolerating missing metadata."""
    metadata = entry.get("metadata") or {} if isinstance(entry, dict) else {}
    return Element(
        category=str(entry.get("type") or "") if isinstance(entry, dict) else "",
        text=(entry.get("text") or "") if isinstance(entry, dict) else "",
        page=metadata.get("page_number"),
        html=metadata.get("text_as_html"),
    )


def _carry_pages(elements: "Sequence[Element]") -> None:
    """Fill in a missing ``page`` from the last element that had one.

    Some elements arrive without ``page_number``; attributing them to the page they follow
    beats dropping their provenance.
    """
    current: int | None = None
    for element in elements:
        if element.page is not None:
            current = element.page
        else:
            element.page = current


def _blocks(elements: "Sequence[Element]") -> list[tuple[int | None, str, bool]]:
    """Render ``elements`` into ``(page, markdown, is_table)`` blocks, in element order.

    Order is the partitioner's, never re-sorted: it is the reading order the layout produced.
    Dropped categories and blocks that render blank do not appear.
    """
    blocks: list[tuple[int | None, str, bool]] = []
    for element in elements:
        if element.category in _DROPPED:
            continue
        if element.category == _TABLE:
            markdown, is_table = _render_table(element), True
        else:
            text = strip_markup(element.text).strip()
            if not text:
                continue
            if element.category in _HEADINGS:
                markdown = f"## {text}"
            elif element.category == _LIST_ITEM:
                markdown = f"- {text}"
            else:
                markdown = text
            is_table = False
        if not markdown.strip():
            continue
        if element.category == _LIST_ITEM and _extends_list(blocks, element.page):
            page, previous, _ = blocks[-1]
            blocks[-1] = (page, f"{previous}\n{markdown}", False)
            continue
        blocks.append((element.page, markdown, is_table))
    return blocks


def _extends_list(
    blocks: "Sequence[tuple[int | None, str, bool]]", page: int | None
) -> bool:
    """Whether a list item continues the previous block instead of starting its own."""
    return bool(blocks) and blocks[-1][0] == page and blocks[-1][1].startswith("- ")


def _render_table(element: Element) -> str:
    """A ``Table`` element as a markdown pipe table.

    Built from ``text_as_html`` when the API inferred structure, and from the element's own
    flattened text when it did not — which is the common case under ``fast``. The first row is
    the header whether or not it was ``<th>``; ``colspan``/``rowspan`` are ignored, so a cell
    is emitted once, where the document put it.
    """
    rows = _parse_table_html(element.html or "")
    if not rows:
        return strip_markup(element.text).strip()
    width = max(len(row) for row in rows)
    padded = [
        [strip_markup(cell).replace("|", r"\|").strip() for cell in row]
        + [""] * (width - len(row))
        for row in rows
    ]
    lines = [_row(padded[0]), _row(["---"] * width)]
    lines.extend(_row(row) for row in padded[1:])
    return "\n".join(lines)


def _row(cells: "Sequence[str]") -> str:
    return "| " + " | ".join(cells) + " |"


def _with_description(table: str, description: str | None) -> str:
    """``table`` with its description as the line above it, italicised.

    Emphasis rather than a plain line so the description reads as a caption and not as a
    stray table row; a blank line separates it from the grid, which is what keeps the
    markdown a valid pipe table. With no description the table is returned untouched.
    """
    if not description:
        return table
    return f"*{description}*\n\n{table}"


class _TableHtmlParser(HTMLParser):
    """Collect ``<tr>``/``<td>`` text out of ``text_as_html``.

    Deliberately stdlib: the rendering has to be identical for the same HTML, because a page
    re-partitioned under ``hi_res`` must not render its tables differently from its neighbours.
    Tags other than rows and cells are ignored, so an ``<img>`` vanishes and an ``<a>`` keeps
    its anchor text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._close_cell()
            self._cell = []
        elif tag in _CELL_BREAKS and self._cell is not None:
            self._cell.append(" ")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        """``<br/>`` arrives here rather than at ``handle_starttag``."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag in _CELL_BREAKS and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        """Flush a row or cell an unclosed tag left open."""
        super().close()
        self._close_row()

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        # Collapse the newlines and runs of spaces an HTML table puts inside a cell.
        assert self._row is not None
        self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _parse_table_html(html: str) -> list[list[str]]:
    """The table's cells as rows, or ``[]`` when ``html`` holds no table at all."""
    if not html.strip():
        return []
    parser = _TableHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup — fall back to the element's own text
        return []
    return parser.rows


def _split_table(markdown: str, max_chars: int, overlap_rows: int = 0) -> list[str]:
    """Split an oversized table by rows, repeating the header — and ``overlap_rows`` — in each piece.

    A table is one chunk, and a chunk is one embedding input: an oversized one fails the whole
    batch and leaves the document with no vectors at all. Each piece stays a valid standalone
    table, so a judge reading only one of them still sees the column names.

    The pieces **overlap**, the way ``chunk_sentences`` overlaps sentence windows: each one
    reopens with the last ``overlap_rows`` rows of the previous piece, so a row that only means
    something read against the one above it is readable in at least one piece. Carried rows are
    dropped when they would leave no room for a new row, so a split always advances.
    """
    if max_chars <= 0 or len(markdown) <= max_chars:
        return [markdown]
    lines = markdown.split("\n")
    if len(lines) < 3:  # not a header + separator + body table; leave it whole
        return [markdown]
    header, separator, body = lines[0], lines[1], lines[2:]
    overhead = len(header) + len(separator) + 2
    overlap = max(0, overlap_rows)
    pieces: list[str] = []
    current: list[str] = []
    size = overhead
    for row in body:
        if current and size + len(row) + 1 > max_chars:
            pieces.append("\n".join([header, separator, *current]))
            current = current[len(current) - overlap :] if overlap else []
            while current and overhead + _rows_size(current) >= max_chars:
                current.pop(0)
            size = overhead + _rows_size(current)
        current.append(row)
        size += len(row) + 1
    if current:
        pieces.append("\n".join([header, separator, *current]))
    logger.debug(
        "extract: tabla de %d carácter(es) partida en %d pieza(s) (máximo %d)",
        len(markdown),
        len(pieces),
        max_chars,
    )
    return pieces or [markdown]


def _rows_size(rows: "Sequence[str]") -> int:
    """The characters ``rows`` add to a piece, newline separators included."""
    return sum(len(row) + 1 for row in rows)
