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
    """Fake markitdown seam: records every (body, file_extension) call.

    ``markdown`` may be a single string (returned for every call) or one string per
    call, so a per-page conversion can be given distinct page text.
    """

    def __init__(self, markdown: str | list[str] = "# md") -> None:
        self.markdown = markdown
        self.calls: list[tuple[bytes, str]] = []

    def __call__(self, body: bytes, file_extension: str) -> str:
        self.calls.append((body, file_extension))
        if isinstance(self.markdown, str):
            return self.markdown
        return self.markdown[len(self.calls) - 1]

    @property
    def body(self) -> bytes | None:
        """Body of the last call (the only one, for a single-conversion document)."""
        return self.calls[-1][0] if self.calls else None

    @property
    def file_extension(self) -> str | None:
        return self.calls[-1][1] if self.calls else None


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
    # Only the requested page is converted, and it reaches the converter alone.
    assert len(conv.calls) == 1
    assert len(PdfReader(BytesIO(conv.body)).pages) == 1
    assert result.page == 2


def test_pdf_without_page_converts_every_page_separately() -> None:
    conv = _RecordingConverter()
    MarkdownContentExtractor(converter=conv).extract(_doc(DocType.PDF, _pdf(3)), _loc(page=None))
    # One conversion per page, each a one-page document — that is what attributes a
    # sentence to its page.
    assert len(conv.calls) == 3
    assert [len(PdfReader(BytesIO(body)).pages) for body, _ in conv.calls] == [1, 1, 1]
    assert {ext for _, ext in conv.calls} == {".pdf"}


def test_pdf_without_page_joins_every_page_into_text() -> None:
    conv = _RecordingConverter(markdown=["uno", "dos", "tres"])
    result = MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.PDF, _pdf(3)), _loc(page=None)
    )
    assert result.text == "uno\n\ndos\n\ntres"
    assert result.page is None


def test_empty_pdf_yields_no_text_and_no_sentences() -> None:
    conv = _RecordingConverter()
    result = MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.PDF, _pdf(0)), _loc(page=None)
    )
    assert conv.calls == []
    assert result.text == ""  # the orchestrator turns this into EMPTY_CONTENT
    assert result.sentences == []


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


def test_links_are_stripped_with_their_anchor_text() -> None:
    conv = _RecordingConverter(
        markdown=(
            "Ver [el informe](https://x.com/a.pdf#page=3) y [otro][ref].\n\n"
            "[ref]: https://y.com/b\n\n"
            "<a href='https://x'>Banxico</a> subió la tasa.\n\n"
            "Fuente <https://x.com> y www.ejemplo.com/x."
        )
    )
    result = MarkdownContentExtractor(converter=conv).extract(_doc(DocType.HTML, b"x"), _loc(DocType.HTML))
    assert "http" not in result.text and "www." not in result.text
    assert "](" not in result.text and "<a " not in result.text
    assert "el informe" not in result.text and "otro" not in result.text  # anchor text gone
    assert "Banxico" not in result.text
    assert "subió la tasa." in result.text


def test_link_markup_never_becomes_a_sentence() -> None:
    conv = _RecordingConverter(markdown="Texto uno. [BMV](https://bmv.com.mx/a) https://otro.mx/b Texto dos.")
    result = MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.HTML, b"x"), _loc(DocType.HTML)
    )
    joined = " ".join(s.text for s in result.sentences)
    assert "http" not in joined and "bmv.com.mx" not in joined
    assert "BMV" not in joined
    assert "Texto uno." in joined and "Texto dos." in joined


def test_space_runs_between_words_are_collapsed() -> None:
    # Two or more horizontal spaces between words collapse to one — tabs and the
    # non-breaking spaces an HTML conversion emits included. Newlines are untouched.
    conv = _RecordingConverter(markdown="Uno    dos\tres\u00a0\u00a0cuatro\ncinco  seis")
    result = MarkdownContentExtractor(converter=conv).extract(_doc(DocType.HTML, b"x"), _loc(DocType.HTML))
    assert result.text == "Uno dos\tres cuatro\ncinco seis"


def test_extracted_content_type() -> None:
    conv = _RecordingConverter()
    result = MarkdownContentExtractor(converter=conv).extract(_doc(DocType.HTML, b"x"), _loc(DocType.HTML))
    assert isinstance(result, ExtractedContent)


# -- sentence segmentation ------------------------------------------------------------------


def test_pdf_sentences_carry_their_page_and_a_document_wide_index() -> None:
    conv = _RecordingConverter(
        markdown=["La BMV cerró al alza. Ganó 0.8%.", "El volumen fue menor."]
    )
    result = MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.PDF, _pdf(2)), _loc(page=None)
    )
    assert [(s.page, s.index, s.text) for s in result.sentences] == [
        (1, 0, "La BMV cerró al alza."),
        (1, 1, "Ganó 0.8%."),
        (2, 2, "El volumen fue menor."),  # index is document-wide, not per page
    ]


def test_selected_pdf_page_sentences_carry_that_page() -> None:
    conv = _RecordingConverter(markdown="Una sola oración.")
    result = MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.PDF, _pdf(3)), _loc(page=2)
    )
    assert [(s.page, s.index) for s in result.sentences] == [(2, 0)]


def test_docx_and_html_sentences_have_no_page() -> None:
    for doc_type, body in ((DocType.DOCX, b"DOCX-BYTES"), (DocType.HTML, b"<p>x</p>")):
        conv = _RecordingConverter(markdown="Primera. Segunda.")
        result = MarkdownContentExtractor(converter=conv).extract(
            _doc(doc_type, body), _loc(doc_type)
        )
        assert [(s.page, s.index, s.text) for s in result.sentences] == [
            (None, 0, "Primera."),
            (None, 1, "Segunda."),
        ]


def test_image_markup_never_becomes_a_sentence() -> None:
    conv = _RecordingConverter(markdown="Texto uno. ![alt](img.png) <img src='y.png'> Texto dos.")
    result = MarkdownContentExtractor(converter=conv).extract(
        _doc(DocType.HTML, b"x"), _loc(DocType.HTML)
    )
    joined = " ".join(s.text for s in result.sentences)
    assert "![" not in joined and "<img" not in joined and "img.png" not in joined


# -- real markitdown integration (skips if the optional extra is absent) --------------------


def test_real_html_conversion_preserves_structure_drops_images_and_links() -> None:
    pytest.importorskip("markitdown")
    html = (
        b"<h1>Titulo</h1>"
        b"<ul><li>uno</li><li>dos</li></ul>"
        b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        b"<img src='x.png'>"
        b"<p>Ver <a href='https://x.com/nota'>la nota</a> en https://x.com/otra.</p>"
    )
    result = MarkdownContentExtractor().extract(_doc(DocType.HTML, html), _loc(DocType.HTML))
    assert "# Titulo" in result.text  # heading
    assert ("* uno" in result.text) or ("- uno" in result.text)  # list
    assert "| A | B |" in result.text  # table
    assert "![" not in result.text and "<img" not in result.text  # no images
    assert "http" not in result.text  # no links
    assert "la nota" not in result.text  # anchor text dropped with the link


# -- backend selection (settings.retrieval_extractor) ----------------------------------------


def _select(monkeypatch, **overrides):
    from types import SimpleNamespace

    from scorekeeper.core.retrieval import extract as module

    values = {"retrieval_extractor": "markdown", "unstructured_api_url": None}
    values.update(overrides)
    settings = SimpleNamespace(**values)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    return module.default_content_extractor()


def test_the_default_backend_is_the_markdown_extractor(monkeypatch) -> None:
    assert isinstance(_select(monkeypatch), MarkdownContentExtractor)


def test_the_unstructured_backend_is_selected_with_an_api_url(monkeypatch) -> None:
    from scorekeeper.core.retrieval import UnstructuredContentExtractor

    extractor = _select(
        monkeypatch,
        retrieval_extractor="unstructured",
        unstructured_api_url="http://unstructured-api:8000",
    )
    assert isinstance(extractor, UnstructuredContentExtractor)


def test_unstructured_without_an_api_url_falls_back_to_markdown(monkeypatch) -> None:
    """A half-configured deployment degrades; it does not fail every retrieval."""
    extractor = _select(monkeypatch, retrieval_extractor="unstructured")
    assert isinstance(extractor, MarkdownContentExtractor)


def test_an_unknown_backend_falls_back_to_markdown(monkeypatch) -> None:
    assert isinstance(_select(monkeypatch, retrieval_extractor="ocr"), MarkdownContentExtractor)
