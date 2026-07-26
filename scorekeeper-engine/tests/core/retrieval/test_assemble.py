"""Tests for the Assemble stage (ExtractedContent → RetrievedDocument → RetrievedContext)."""

from __future__ import annotations

from scorekeeper.core.retrieval.types import (
    AuthDecision,
    AuthRequirement,
    AuthStatus,
    DocType,
    DocumentLocator,
    ExtractedContent,
    RetrievalOutcome,
    RetrievalReport,
    RetrievalStatus,
    SourceFormat,
    SourceRef,
)


def _ref(rank: int, *, name: str = "cognos", url: str = "https://h/Rep.pdf#page=3") -> SourceRef:
    return SourceRef(name=name, url=url, rank=rank)


def _loc(*, filename: str = "Rep.pdf", url: str = "https://h/Rep.pdf") -> DocumentLocator:
    return DocumentLocator(
        document_url=url, filename=filename, doc_type=DocType.PDF, host="h", page=3
    )


# -- to_document ----------------------------------------------------------------------------


def test_to_document_maps_fields() -> None:
    doc = ExtractedContent(text="# Titulo\n- a", page=3).to_document(_ref(0), _loc())
    assert doc.name == "cognos"
    assert doc.document == "Rep.pdf"
    assert doc.content == "# Titulo\n- a"
    # url keeps the #page anchor from the source reference (not the fetch key).
    assert doc.url == "https://h/Rep.pdf#page=3"


def test_to_document_falls_back_when_filename_empty() -> None:
    # A /-terminated URL yields an empty filename → fall back to the reference label.
    doc = ExtractedContent(text="x").to_document(_ref(0, name="El Economista"), _loc(filename=""))
    assert doc.document == "El Economista"


def test_to_document_falls_back_to_url_when_no_label() -> None:
    doc = ExtractedContent(text="x").to_document(
        _ref(0, name="", url=""), _loc(filename="", url="https://h/doc")
    )
    assert doc.document == "https://h/doc"


# -- assembled ------------------------------------------------------------------------------


def test_assembled_non_empty_is_retrieved() -> None:
    auth = AuthDecision(requirement=AuthRequirement.PUBLIC, status=AuthStatus.NOT_NEEDED)
    outcome = RetrievalOutcome.assembled(_ref(0), _loc(), ExtractedContent(text="# md"), auth=auth)
    assert outcome.status is RetrievalStatus.RETRIEVED
    assert outcome.document is not None
    assert outcome.document.content == "# md"
    assert outcome.locator is not None  # threaded through
    assert outcome.auth is auth  # threaded through
    assert outcome.error is None


def test_assembled_blank_is_empty_content() -> None:
    outcome = RetrievalOutcome.assembled(_ref(0), _loc(), ExtractedContent(text="   \n  "))
    assert outcome.status is RetrievalStatus.EMPTY_CONTENT
    assert outcome.document is None
    assert outcome.error == "contenido vacío"


# -- to_context (collection) ----------------------------------------------------------------


def test_to_context_excludes_empty_preserves_order_and_duplicates() -> None:
    a = RetrievalOutcome.assembled(_ref(0, name="A"), _loc(), ExtractedContent(text="doc A"))
    empty = RetrievalOutcome.assembled(_ref(1, name="B"), _loc(), ExtractedContent(text=""))
    a_dup = RetrievalOutcome.assembled(_ref(2, name="A"), _loc(), ExtractedContent(text="doc A"))
    c = RetrievalOutcome.assembled(_ref(3, name="C"), _loc(), ExtractedContent(text="doc C"))

    report = RetrievalReport(source_format=SourceFormat.JSON_INDEXED, outcomes=[a, empty, a_dup, c])
    context = report.to_context()

    # EMPTY_CONTENT dropped; the duplicate kept; rank order preserved.
    assert [d.name for d in context.documents] == ["A", "A", "C"]
    assert len(context.documents) == 3


def test_to_context_markdown_survives_into_judge_text() -> None:
    outcome = RetrievalOutcome.assembled(
        _ref(0, name="Reporte"), _loc(), ExtractedContent(text="# Titulo\n\n| A | B |\n| --- | --- |")
    )
    report = RetrievalReport(source_format=SourceFormat.PIPE_LABELLED, outcomes=[outcome])
    context = report.to_context()
    rendered = context.render()
    assert "# Titulo" in rendered
    assert "| A | B |" in rendered
    # node_texts feeds groundedness metrics; the markdown must be present there too.
    assert any("# Titulo" in node for node in context.node_texts())
