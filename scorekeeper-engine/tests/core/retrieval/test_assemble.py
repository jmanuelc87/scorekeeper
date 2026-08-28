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
    Sentence,
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
    # No whole-document text survives assemble: the sentences do, and the embedding
    # phase turns them into the chunks a judge eventually reads.
    assert doc.content == ""
    # url keeps the #page anchor from the source reference (not the fetch key).
    assert doc.url == "https://h/Rep.pdf#page=3"


def test_to_document_carries_the_segmented_sentences() -> None:
    sentences = [Sentence(page=3, index=0, text="Una.")]
    doc = ExtractedContent(text="Una.", page=3, sentences=sentences).to_document(_ref(0), _loc())
    assert doc.sentences == sentences


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
    assert outcome.document.document == "Rep.pdf"
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


def test_to_context_carries_sentences_not_markdown() -> None:
    """Assemble hands on the segmented sentences; the markdown blob stops here.

    Nothing is renderable yet — a document only gains judge-readable text once the
    embedding phase has chunked those sentences.
    """
    extracted = ExtractedContent(
        text="# Titulo\n\nUna oración. Otra oración.",
        sentences=[
            Sentence(page=1, index=0, text="Una oración."),
            Sentence(page=1, index=1, text="Otra oración."),
        ],
    )
    outcome = RetrievalOutcome.assembled(_ref(0, name="Reporte"), _loc(), extracted)
    report = RetrievalReport(source_format=SourceFormat.PIPE_LABELLED, outcomes=[outcome])
    context = report.to_context()
    assert [s.text for s in context.documents[0].sentences] == [
        "Una oración.",
        "Otra oración.",
    ]
    # Only the header renders: there are no chunks yet.
    assert context.render() == "Reporte (Rep.pdf — https://h/Rep.pdf#page=3)"
    assert context.node_texts() == ["Rep.pdf"]
