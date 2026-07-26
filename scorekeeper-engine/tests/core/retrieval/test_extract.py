"""Tests for the markdown content extractor (extract stage)."""

from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from scorekeeper.core.retrieval import (
    ContentExtractor,
    ExtractError,
    MarkdownContentExtractor,
    PageNotFoundError,
)
from scorekeeper.core.retrieval.types import DocType, DocumentLocator, ExtractedContent, FetchedDocument


class _RecordingConverter:
    """Fake markitdown seam: records (body, file_extension) and returns canned markdown."""

    def __init__(self, markdown: str = "# md") -> None:
        self.markdown = markdown
        self.body: bytes | None = None
        self.file_extension: str | None = None

    def __call__(self, body: bytes, file_extension: str) -> str:
        self.body = body
        self.file_extension = file_extension
        return self.markdown


def _doc(doc_type: DocType, body: bytes) -> FetchedDocument:
    return FetchedDocument(document_url="https://h/x", doc_type=doc_type, body=body)


def _loc(doc_type: DocType = DocType.PDF, *, page: int | None = None) -> DocumentLocator:
    return DocumentLocator(
        document_url="https://h/x", filename="x", doc_type=doc_type, host="h", page=page
    )


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# -- protocol / support ---------------------------------------------------------------------


def test_conforms_to_protocol() -> None:
    assert isinstance(MarkdownContentExtractor(), ContentExtractor)


def test_supports() -> None:
    ext = MarkdownContentExtractor()
    assert ext.supports(DocType.PDF)
    assert ext.supports(DocType.DOCX)
    assert ext.supports(DocType.HTML)
    assert not ext.supports(DocType.UNKNOWN)


# -- dispatch / page slicing (logic we own; converter injected) -----------------------------


def test_pdf_page_is_sliced_to_one_page() -> None:
    conv = _RecordingConverter()
    result = MarkdownContentExtractor(converter=conv).extract(_doc(DocType.PDF, _pdf(3)), _loc(page=2))
    assert conv.file_extension == ".pdf"
    # The converter receives just the requested page.
    assert len(PdfReader(BytesIO(conv.body)).pages) == 1
    assert result.page == 2


def test_pdf_without_page_converts_whole_document() -> None:
    conv = _RecordingConverter()
    MarkdownContentExtractor(converter=conv).extract(_doc(DocType.PDF, _pdf(3)), _loc(page=None))
    assert len(PdfReader(BytesIO(conv.body)).pages) == 3


def test_pdf_page_out_of_range_raises() -> None:
    conv = _RecordingConverter()
    with pytest.raises(PageNotFoundError):
        MarkdownContentExtractor(converter=conv).extract(_doc(DocType.PDF, _pdf(3)), _loc(page=99))


def test_malformed_pdf_raises_extract_error() -> None:
    conv = _RecordingConverter()
    with pytest.raises(ExtractError):
        MarkdownContentExtractor(converter=conv).extract(_doc(DocType.PDF, b"not-a-pdf"), _loc(page=1))


def test_docx_converts_whole_body_ignoring_page() -> None:
    conv = _RecordingConverter()
    MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.DOCX, b"DOCX-BYTES"), _loc(DocType.DOCX, page=5)
    )
    assert conv.file_extension == ".docx"
    assert conv.body == b"DOCX-BYTES"  # whole document; page selector ignored


def test_html_converts_whole_body() -> None:
    conv = _RecordingConverter()
    MarkdownContentExtractor(converter=conv).extract(_doc(DocType.HTML, b"<h1>x</h1>"), _loc(DocType.HTML))
    assert conv.file_extension == ".html"
    assert conv.body == b"<h1>x</h1>"


def test_unsupported_type_raises() -> None:
    conv = _RecordingConverter()
    with pytest.raises(ExtractError):
        MarkdownContentExtractor(converter=conv).extract(_doc(DocType.UNKNOWN, b"x"), _loc(DocType.UNKNOWN))


def test_images_are_stripped_and_text_stripped() -> None:
    conv = _RecordingConverter(markdown="\n# Title\n\n![alt](img.png)\n\n<img src='y.png'>\n\ntext\n\n")
    result = MarkdownContentExtractor(converter=conv).extract(_doc(DocType.HTML, b"x"), _loc(DocType.HTML))
    assert "![" not in result.text
    assert "<img" not in result.text
    assert result.text.startswith("# Title")  # leading/trailing whitespace stripped
    assert "text" in result.text


def test_extracted_content_type() -> None:
    conv = _RecordingConverter()
    result = MarkdownContentExtractor(converter=conv).extract(_doc(DocType.HTML, b"x"), _loc(DocType.HTML))
    assert isinstance(result, ExtractedContent)


# -- real markitdown integration (skips if the optional extra is absent) --------------------


def test_real_html_conversion_preserves_structure_drops_images() -> None:
    pytest.importorskip("markitdown")
    html = (
        b"<h1>Titulo</h1>"
        b"<ul><li>uno</li><li>dos</li></ul>"
        b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        b"<img src='x.png'>"
    )
    result = MarkdownContentExtractor().extract(_doc(DocType.HTML, html), _loc(DocType.HTML))
    assert "# Titulo" in result.text  # heading
    assert ("* uno" in result.text) or ("- uno" in result.text)  # list
    assert "| A | B |" in result.text  # table
    assert "![" not in result.text and "<img" not in result.text  # no images
