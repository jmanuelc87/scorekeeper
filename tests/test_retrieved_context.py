"""Tests for the structured retrieved-context entity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scorekeeper.retrieved_context import RetrievedContext, RetrievedDocument


def test_document_node_text_joins_source_and_content() -> None:
    doc = RetrievedDocument(
        name="Política §3", document="manual.pdf", content="Reembolso en 30 días."
    )
    assert doc.node_text() == "manual.pdf\nReembolso en 30 días."


def test_document_node_text_strips_when_source_missing() -> None:
    doc = RetrievedDocument(name="", document="", content="solo contenido")
    assert doc.node_text() == "solo contenido"


def test_node_texts_preserve_order_and_drop_empty() -> None:
    context = RetrievedContext(
        documents=[
            RetrievedDocument(name="a", document="d1", content="uno"),
            RetrievedDocument(name="b", document="", content=""),
            RetrievedDocument(name="c", document="d2", content="dos"),
        ]
    )
    # Rank order kept; the empty-node document is dropped.
    assert context.node_texts() == ["d1\nuno", "d2\ndos"]


def test_is_empty() -> None:
    assert RetrievedContext().is_empty
    assert not RetrievedContext(
        documents=[RetrievedDocument(name="", document="", content="x")]
    ).is_empty


def test_render_includes_label_source_url_and_content() -> None:
    context = RetrievedContext(
        documents=[
            RetrievedDocument(
                name="Política de reembolsos",
                document="manual.pdf",
                content="Reembolso en 30 días.",
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


def test_from_blob_blank_line_splits_into_content_only_docs() -> None:
    context = RetrievedContext.from_blob("doc uno\ncon dos líneas\n\ndoc dos")
    assert context.documents == [
        RetrievedDocument(name="", document="", content="doc uno\ncon dos líneas"),
        RetrievedDocument(name="", document="", content="doc dos"),
    ]


def test_from_blob_empty_and_whitespace_yield_no_docs() -> None:
    assert RetrievedContext.from_blob("").documents == []
    assert RetrievedContext.from_blob("  \n  ").documents == []
    assert RetrievedContext.from_blob(None).documents == []


def test_from_blob_single_document() -> None:
    context = RetrievedContext.from_blob("único documento")
    assert context.node_texts() == ["único documento"]


def test_model_validate_round_trip() -> None:
    payload = {
        "documents": [
            {
                "name": "n",
                "document": "d",
                "content": "c",
                "url": "https://ejemplo.com",
            }
        ]
    }
    context = RetrievedContext.model_validate(payload)
    assert context.model_dump() == payload


def test_url_defaults_to_none() -> None:
    doc = RetrievedDocument(name="n", document="d", content="c")
    assert doc.url is None


def test_required_fields_enforced() -> None:
    with pytest.raises(ValidationError):
        RetrievedDocument(name="n", content="c")  # missing ``document``
