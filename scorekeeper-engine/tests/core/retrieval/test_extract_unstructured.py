"""Tests for the unstructured-api content extractor (extract stage)."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from scorekeeper.core.retrieval import (
    ContentExtractor,
    ExtractError,
    PageNotFoundError,
    UnstructuredContentExtractor,
)
from scorekeeper.core.retrieval import extract_unstructured as module
from scorekeeper.core.retrieval.extract_unstructured import Element
from scorekeeper.core.retrieval.types import DocType, DocumentLocator, FetchedDocument


class _RecordingPartitioner:
    """Fake unstructured-api seam: records every (body, filename, strategy) call.

    ``elements`` is either one list returned for every call, or one list per call so the
    fast pass and each hi_res escalation can answer differently.
    """

    def __init__(self, elements: list[Element] | list[list[Element]]) -> None:
        self.elements = elements
        self.calls: list[tuple[bytes, str, str]] = []

    def __call__(self, body: bytes, *, filename: str, strategy: str) -> list[Element]:
        self.calls.append((body, filename, strategy))
        if not self.elements or isinstance(self.elements[0], Element):
            batch = self.elements
        else:
            batch = self.elements[len(self.calls) - 1]
        # A fresh copy per call: the extractor mutates ``page`` in place.
        return [element.model_copy() for element in batch]

    @property
    def strategies(self) -> list[str]:
        return [call[2] for call in self.calls]

    @property
    def bodies(self) -> list[bytes]:
        return [call[0] for call in self.calls]


def _settings(**overrides):
    """The extractor's settings, defaulted to "fast, no OCR, no table splitting"."""
    values = {
        "unstructured_strategy": "fast",
        "unstructured_ocr_fallback": False,
        "unstructured_max_ocr_pages": 20,
        "extract_table_max_chars": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.setattr(module, "get_settings", _settings)


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


def _extract(elements, *, doc_type=DocType.PDF, body=None, page=None, **settings):
    """Run the extractor over ``elements``, returning (content, partitioner)."""
    if settings:
        module.get_settings = lambda: _settings(**settings)  # noqa: E731
    partitioner = _RecordingPartitioner(elements)
    extractor = UnstructuredContentExtractor(partitioner=partitioner)
    body = _pdf(1) if body is None and doc_type is DocType.PDF else (body or b"x")
    content = extractor.extract(_doc(doc_type, body), _loc(doc_type, page=page))
    return content, partitioner


# -- protocol / support ---------------------------------------------------------------------


def test_conforms_to_protocol() -> None:
    assert isinstance(UnstructuredContentExtractor(), ContentExtractor)


@pytest.mark.parametrize(
    ("doc_type", "supported"),
    [
        (DocType.PDF, True),
        (DocType.DOCX, True),
        (DocType.HTML, True),
        (DocType.UNKNOWN, False),
    ],
)
def test_supports_the_three_convertible_types(doc_type, supported) -> None:
    assert UnstructuredContentExtractor().supports(doc_type) is supported


def test_an_unsupported_type_raises_rather_than_partitioning() -> None:
    partitioner = _RecordingPartitioner([])
    extractor = UnstructuredContentExtractor(partitioner=partitioner)
    with pytest.raises(ExtractError):
        extractor.extract(_doc(DocType.UNKNOWN, b"x"), _loc(DocType.UNKNOWN))
    assert partitioner.calls == []


# -- element rendering ----------------------------------------------------------------------


def test_a_title_becomes_a_heading_and_narrative_stays_verbatim() -> None:
    content, _ = _extract(
        [
            Element(category="Title", text="Resumen anual", page=1),
            Element(category="NarrativeText", text="La empresa creció.", page=1),
        ]
    )
    assert content.text == "## Resumen anual\n\nLa empresa creció."


def test_consecutive_list_items_render_as_one_list() -> None:
    content, _ = _extract(
        [
            Element(category="ListItem", text="Uno", page=1),
            Element(category="ListItem", text="Dos", page=1),
            Element(category="NarrativeText", text="Fin.", page=1),
        ]
    )
    assert content.text == "- Uno\n- Dos\n\nFin."


def test_an_unknown_category_is_treated_as_narrative() -> None:
    """Uncategorised body text is common; dropping it would lose real content."""
    content, _ = _extract([Element(category="UncategorizedText", text="Texto.", page=1)])
    assert content.text == "Texto."


@pytest.mark.parametrize(
    "category", ["Header", "Footer", "PageNumber", "PageBreak", "Image", "FigureCaption"]
)
def test_page_furniture_is_dropped_by_category(category) -> None:
    content, _ = _extract(
        [
            Element(category=category, text="Ruido", page=1),
            Element(category="NarrativeText", text="Contenido.", page=1),
        ]
    )
    assert content.text == "Contenido."
    assert [s.text for s in content.sentences] == ["Contenido."]


def test_a_blank_element_produces_no_block() -> None:
    content, _ = _extract(
        [
            Element(category="NarrativeText", text="   ", page=1),
            Element(category="NarrativeText", text="Algo.", page=1),
        ]
    )
    assert content.text == "Algo."


def test_a_bare_url_is_stripped_out_of_narrative() -> None:
    content, _ = _extract(
        [Element(category="NarrativeText", text="Ver https://ejemplo.com hoy.", page=1)]
    )
    assert "ejemplo.com" not in content.text


def test_no_elements_yields_empty_text() -> None:
    """Blank text is the assemble stage's EMPTY_CONTENT gate."""
    content, _ = _extract([])
    assert content.text == ""
    assert content.sentences == []


# -- tables ---------------------------------------------------------------------------------


_TABLE_HTML = (
    "<table><thead><tr><th>Año</th><th>Ingreso</th></tr></thead>"
    "<tbody><tr><td>2023</td><td>1.5</td></tr><tr><td>2024</td><td>2.0</td></tr>"
    "</tbody></table>"
)


def test_a_table_renders_as_a_pipe_table() -> None:
    content, _ = _extract(
        [Element(category="Table", text="ignorado", page=1, html=_TABLE_HTML)]
    )
    assert content.text == (
        "| Año | Ingreso |\n| --- | --- |\n| 2023 | 1.5 |\n| 2024 | 2.0 |"
    )


def test_a_table_is_exactly_one_atomic_sentence() -> None:
    content, _ = _extract(
        [Element(category="Table", text="ignorado", page=3, html=_TABLE_HTML)]
    )
    assert len(content.sentences) == 1
    assert content.sentences[0].atomic is True
    assert content.sentences[0].page == 3


def test_narrative_sentences_are_not_atomic() -> None:
    content, _ = _extract([Element(category="NarrativeText", text="Una.", page=1)])
    assert content.sentences[0].atomic is False


def test_a_table_without_html_falls_back_to_its_text() -> None:
    """The common case under the fast strategy, which infers no table structure."""
    content, _ = _extract([Element(category="Table", text="Año Ingreso", page=1)])
    assert content.text == "Año Ingreso"
    assert content.sentences[0].atomic is True


def test_ragged_rows_are_padded_to_the_widest() -> None:
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
    content, _ = _extract([Element(category="Table", text="", page=1, html=html)])
    assert content.text.splitlines()[-1] == "| d |  |  |"


def test_a_pipe_inside_a_cell_is_escaped() -> None:
    html = "<table><tr><td>a|b</td></tr></table>"
    content, _ = _extract([Element(category="Table", text="", page=1, html=html)])
    assert content.text.splitlines()[0] == r"| a\|b |"


def test_cell_whitespace_and_markup_are_collapsed() -> None:
    html = "<table><tr><td>  Año\n  fiscal </td><td><img src='x'>2024</td></tr></table>"
    content, _ = _extract([Element(category="Table", text="", page=1, html=html)])
    assert content.text.splitlines()[0] == "| Año fiscal | 2024 |"


def test_html_with_no_table_falls_back_to_the_element_text() -> None:
    content, _ = _extract(
        [Element(category="Table", text="respaldo", page=1, html="<p>nada</p>")]
    )
    assert content.text == "respaldo"


def test_an_oversized_table_is_split_repeating_the_header() -> None:
    rows = "".join(f"<tr><td>{i}</td><td>valor {i}</td></tr>" for i in range(40))
    html = f"<table><tr><th>Id</th><th>Valor</th></tr>{rows}</table>"
    content, _ = _extract(
        [Element(category="Table", text="", page=1, html=html)],
        extract_table_max_chars=200,
    )
    assert len(content.sentences) > 1
    assert all(s.atomic for s in content.sentences)
    assert all(s.text.startswith("| Id | Valor |\n| --- | --- |") for s in content.sentences)


def test_a_table_under_the_limit_stays_one_sentence() -> None:
    content, _ = _extract(
        [Element(category="Table", text="", page=1, html=_TABLE_HTML)],
        extract_table_max_chars=4000,
    )
    assert len(content.sentences) == 1


# -- pages and sentence indexing ------------------------------------------------------------


def test_sentences_carry_the_page_their_element_came_from() -> None:
    content, _ = _extract(
        [
            Element(category="NarrativeText", text="Una.", page=1),
            Element(category="NarrativeText", text="Dos.", page=2),
        ]
    )
    assert [(s.page, s.text) for s in content.sentences] == [(1, "Una."), (2, "Dos.")]


def test_the_sentence_index_runs_across_the_whole_document() -> None:
    content, _ = _extract(
        [
            Element(category="NarrativeText", text="Una. Dos.", page=1),
            Element(category="NarrativeText", text="Tres. Cuatro.", page=2),
        ]
    )
    assert [s.index for s in content.sentences] == [0, 1, 2, 3]


def test_an_element_without_a_page_inherits_the_previous_one() -> None:
    content, _ = _extract(
        [
            Element(category="NarrativeText", text="Una.", page=2),
            Element(category="NarrativeText", text="Dos.", page=None),
        ]
    )
    assert [s.page for s in content.sentences] == [2, 2]


def test_a_whole_pdf_is_partitioned_in_one_call() -> None:
    """The point of the change: no per-page conversion loop when there is no fragment."""
    body = _pdf(5)
    _, partitioner = _extract(
        [Element(category="NarrativeText", text="Una.", page=1)], body=body
    )
    assert len(partitioner.calls) == 1
    assert partitioner.bodies[0] == body
    assert partitioner.calls[0][1] == "documento.pdf"


# -- the #page=N filter ---------------------------------------------------------------------


def test_a_page_reference_posts_only_that_page() -> None:
    _, partitioner = _extract(
        [Element(category="NarrativeText", text="Una.", page=1)], body=_pdf(5), page=3
    )
    assert len(partitioner.calls) == 1
    assert len(PdfReader(BytesIO(partitioner.bodies[0])).pages) == 1


def test_a_page_reference_overrides_the_page_the_slice_reports() -> None:
    """A one-page slice always comes back as page 1; the requested page must win."""
    content, _ = _extract(
        [Element(category="NarrativeText", text="Una.", page=1)], body=_pdf(9), page=7
    )
    assert [s.page for s in content.sentences] == [7]
    assert content.page == 7


def test_a_page_outside_the_document_raises_before_any_call() -> None:
    partitioner = _RecordingPartitioner([])
    extractor = UnstructuredContentExtractor(partitioner=partitioner)
    with pytest.raises(PageNotFoundError):
        extractor.extract(_doc(DocType.PDF, _pdf(2)), _loc(page=5))
    assert partitioner.calls == []


def test_a_malformed_pdf_raises_extract_error() -> None:
    partitioner = _RecordingPartitioner([])
    extractor = UnstructuredContentExtractor(partitioner=partitioner)
    with pytest.raises(ExtractError):
        extractor.extract(_doc(DocType.PDF, b"no soy un pdf"), _loc())
    assert partitioner.calls == []


def test_a_partitioner_failure_surfaces_as_extract_error() -> None:
    def failing(body, *, filename, strategy):
        raise ExtractError("servicio caído")

    extractor = UnstructuredContentExtractor(partitioner=failing)
    with pytest.raises(ExtractError):
        extractor.extract(_doc(DocType.PDF, _pdf(1)), _loc())


# -- DOCX and HTML --------------------------------------------------------------------------


@pytest.mark.parametrize("doc_type", [DocType.DOCX, DocType.HTML])
def test_docx_and_html_sentences_carry_no_page(doc_type) -> None:
    """Neither format has page boundaries, whatever the API reports."""
    content, partitioner = _extract(
        [Element(category="NarrativeText", text="Una. Dos.", page=1)],
        doc_type=doc_type,
        body=b"bytes",
    )
    assert [s.page for s in content.sentences] == [None, None]
    assert partitioner.bodies == [b"bytes"]


def test_html_is_never_sliced_or_escalated() -> None:
    _, partitioner = _extract(
        [Element(category="NarrativeText", text="Una.")],
        doc_type=DocType.HTML,
        body=b"<p>x</p>",
        unstructured_ocr_fallback=True,
    )
    assert partitioner.strategies == ["fast"]


# -- the fast -> hi_res OCR fallback ---------------------------------------------------------


def test_a_page_without_text_is_re_posted_as_hi_res() -> None:
    content, partitioner = _extract(
        [
            [Element(category="NarrativeText", text="Una.", page=1)],  # page 2 empty
            [Element(category="NarrativeText", text="Escaneada.", page=1)],  # the OCR
        ],
        body=_pdf(2),
        unstructured_ocr_fallback=True,
    )
    assert partitioner.strategies == ["fast", "hi_res"]
    assert len(PdfReader(BytesIO(partitioner.bodies[1])).pages) == 1
    assert [(s.page, s.text) for s in content.sentences] == [
        (1, "Una."),
        (2, "Escaneada."),
    ]


def test_pages_that_already_have_text_are_never_re_posted() -> None:
    _, partitioner = _extract(
        [
            Element(category="NarrativeText", text="Una.", page=1),
            Element(category="NarrativeText", text="Dos.", page=2),
        ],
        body=_pdf(2),
        unstructured_ocr_fallback=True,
    )
    assert partitioner.strategies == ["fast"]


def test_the_fallback_can_be_switched_off() -> None:
    _, partitioner = _extract(
        [Element(category="NarrativeText", text="Una.", page=1)],
        body=_pdf(3),
        unstructured_ocr_fallback=False,
    )
    assert partitioner.strategies == ["fast"]


def test_too_many_empty_pages_skips_the_fallback_wholesale() -> None:
    """A fully scanned 300-page PDF must not occupy a worker for an hour."""
    _, partitioner = _extract(
        [Element(category="NarrativeText", text="Una.", page=1)],
        body=_pdf(6),
        unstructured_ocr_fallback=True,
        unstructured_max_ocr_pages=2,
    )
    assert partitioner.strategies == ["fast"]


def test_a_failed_ocr_call_leaves_the_page_empty_instead_of_failing() -> None:
    calls: list[str] = []

    def partitioner(body, *, filename, strategy):
        calls.append(strategy)
        if strategy == "hi_res":
            raise ExtractError("OCR caído")
        return [Element(category="NarrativeText", text="Una.", page=1)]

    module.get_settings = lambda: _settings(unstructured_ocr_fallback=True)  # noqa: E731
    extractor = UnstructuredContentExtractor(partitioner=partitioner)
    content = extractor.extract(_doc(DocType.PDF, _pdf(2)), _loc())
    assert calls == ["fast", "hi_res"]
    assert [s.text for s in content.sentences] == ["Una."]


def test_a_hi_res_base_strategy_never_escalates() -> None:
    _, partitioner = _extract(
        [Element(category="NarrativeText", text="Una.", page=1)],
        body=_pdf(2),
        unstructured_strategy="hi_res",
        unstructured_ocr_fallback=True,
    )
    assert partitioner.strategies == ["hi_res"]


def test_a_page_referenced_scan_escalates_precisely() -> None:
    content, partitioner = _extract(
        [[], [Element(category="NarrativeText", text="Escaneada.", page=1)]],
        body=_pdf(9),
        page=4,
        unstructured_ocr_fallback=True,
    )
    assert partitioner.strategies == ["fast", "hi_res"]
    assert [(s.page, s.text) for s in content.sentences] == [(4, "Escaneada.")]


# -- the default HTTP partitioner ------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, *, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Records the one POST the partitioner makes."""

    def __init__(self, response) -> None:
        self.response = response
        self.kwargs: dict = {}
        self.url: str | None = None

    def post(self, url, **kwargs):
        self.url, self.kwargs = url, kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _api_settings(**overrides):
    values = {
        "unstructured_api_url": "http://unstructured-api:8000/",
        "unstructured_api_key": None,
        "unstructured_ocr_languages": "spa,eng",
        "unstructured_timeout_seconds": 300.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _partitioner(monkeypatch, response, **overrides):
    monkeypatch.setattr(module, "get_settings", lambda: _api_settings(**overrides))
    client = _FakeHttpClient(response)
    return module._ApiPartitioner(client=client), client


def test_the_api_partitioner_posts_the_document_to_the_general_endpoint(monkeypatch) -> None:
    partitioner, client = _partitioner(monkeypatch, _FakeResponse([]))
    partitioner(b"%PDF-1.3", filename="documento.pdf", strategy="hi_res")
    assert client.url == "http://unstructured-api:8000/general/v0/general"
    assert client.kwargs["files"]["files"] == (
        "documento.pdf",
        b"%PDF-1.3",
        "application/pdf",
    )


def test_the_form_carries_the_strategy_and_repeats_the_languages(monkeypatch) -> None:
    """A list value is how a multipart form repeats a field; pairs are rejected by httpx2."""
    partitioner, client = _partitioner(monkeypatch, _FakeResponse([]))
    partitioner(b"x", filename="documento.pdf", strategy="hi_res")
    form = client.kwargs["data"]
    assert form["strategy"] == "hi_res"
    assert form["languages"] == ["spa", "eng"]
    assert form["pdf_infer_table_structure"] == "true"


def test_the_form_encodes_against_a_real_request(monkeypatch) -> None:
    """Guards the one thing a fake client cannot: that httpx2 accepts this shape."""
    import httpx2

    partitioner, client = _partitioner(monkeypatch, _FakeResponse([]))
    partitioner(b"%PDF-1.3", filename="documento.pdf", strategy="fast")
    body = httpx2.Request("POST", client.url, **client.kwargs).read()
    assert b'name="languages"' in body and b"spa" in body


def test_no_api_key_header_when_none_is_configured(monkeypatch) -> None:
    partitioner, client = _partitioner(monkeypatch, _FakeResponse([]))
    partitioner(b"x", filename="documento.html", strategy="fast")
    assert client.kwargs["headers"] == {}


def test_an_api_key_is_sent_as_a_header(monkeypatch) -> None:
    partitioner, client = _partitioner(
        monkeypatch, _FakeResponse([]), unstructured_api_key="secreto"
    )
    partitioner(b"x", filename="documento.html", strategy="fast")
    assert client.kwargs["headers"] == {"unstructured-api-key": "secreto"}


def test_the_response_elements_are_mapped_onto_the_seam(monkeypatch) -> None:
    payload = [
        {
            "type": "Title",
            "text": "Resumen",
            "metadata": {"page_number": 2},
        },
        {
            "type": "Table",
            "text": "plano",
            "metadata": {"page_number": 2, "text_as_html": "<table></table>"},
        },
        {"type": "NarrativeText", "text": "Sin metadatos"},
    ]
    partitioner, _ = _partitioner(monkeypatch, _FakeResponse(payload))
    elements = partitioner(b"x", filename="documento.pdf", strategy="fast")
    assert [(e.category, e.page) for e in elements] == [
        ("Title", 2),
        ("Table", 2),
        ("NarrativeText", None),
    ]
    assert elements[1].html == "<table></table>"


def test_a_transport_failure_becomes_an_extract_error(monkeypatch) -> None:
    partitioner, _ = _partitioner(monkeypatch, RuntimeError("conexión rechazada"))
    with pytest.raises(ExtractError, match="unstructured-api"):
        partitioner(b"x", filename="documento.pdf", strategy="fast")


def test_an_error_status_becomes_an_extract_error(monkeypatch) -> None:
    partitioner, _ = _partitioner(monkeypatch, _FakeResponse([], status=503))
    with pytest.raises(ExtractError):
        partitioner(b"x", filename="documento.pdf", strategy="fast")


def test_a_response_that_is_not_a_list_becomes_an_extract_error(monkeypatch) -> None:
    partitioner, _ = _partitioner(monkeypatch, _FakeResponse({"detail": "nope"}))
    with pytest.raises(ExtractError):
        partitioner(b"x", filename="documento.pdf", strategy="fast")
