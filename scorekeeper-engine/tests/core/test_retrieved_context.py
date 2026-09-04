"""Tests for the structured retrieved-context entity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scorekeeper.core.retrieved_context import Chunk, RetrievedContext, RetrievedDocument


def _doc(*texts: str, name: str = "", document: str = "", url: str | None = None) -> RetrievedDocument:
    """A document whose text is ``texts``, one chunk each, in order."""
    return RetrievedDocument(
        name=name,
        document=document,
        url=url,
        chunks=[Chunk(index=i, text=t) for i, t in enumerate(texts)],
    )


def test_document_content_joins_its_chunks() -> None:
    assert _doc("uno", "dos").content == "uno\ndos"


def test_document_node_text_joins_source_and_content() -> None:
    doc = _doc("Reembolso en 30 días.", name="Política §3", document="manual.pdf")
    assert doc.node_text() == "manual.pdf\nReembolso en 30 días."


def test_document_node_text_strips_when_source_missing() -> None:
    assert _doc("solo contenido").node_text() == "solo contenido"


def test_document_without_chunks_has_no_node_text() -> None:
    """A document the embedding phase could not process carries no text at all."""
    assert RetrievedDocument(name="n", document="").node_text() == ""


def test_node_texts_preserve_order_and_drop_empty() -> None:
    context = RetrievedContext(
        documents=[
            _doc("uno", name="a", document="d1"),
            _doc(name="b", document=""),
            _doc("dos", name="c", document="d2"),
        ]
    )
    # Rank order kept; the empty-node document is dropped.
    assert context.node_texts() == ["d1\nuno", "d2\ndos"]


def test_is_empty() -> None:
    assert RetrievedContext().is_empty
    assert not RetrievedContext(documents=[_doc("x")]).is_empty


def test_render_includes_label_source_url_and_content() -> None:
    context = RetrievedContext(
        documents=[
            _doc(
                "Reembolso en 30 días.",
                name="Política de reembolsos",
                document="manual.pdf",
                url="https://ejemplo.com/manual",
            )
        ]
    )
    rendered = context.render()
    assert "Política de reembolsos" in rendered
    assert "manual.pdf" in rendered
    assert "https://ejemplo.com/manual" in rendered
    assert "Reembolso en 30 días." in rendered


def test_render_empty_is_blank() -> None:
    assert RetrievedContext().render() == ""


def test_model_validate_round_trip() -> None:
    payload = {
        "documents": [
            {
                "name": "n",
                "document": "d",
                "url": "https://ejemplo.com",
                "sentences": [{"page": 1, "index": 0, "text": "c", "atomic": False}],
                "chunks": [{"index": 0, "text": "c"}],
            }
        ]
    }
    context = RetrievedContext.model_validate(payload)
    assert context.model_dump() == payload


def test_url_defaults_to_none() -> None:
    assert _doc("c", name="n", document="d").url is None


def test_required_fields_enforced() -> None:
    with pytest.raises(ValidationError):
        RetrievedDocument(name="n")  # missing ``document``
