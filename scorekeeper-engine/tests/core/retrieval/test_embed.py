"""Tests for the retrieval pipeline's own OpenAI embedder."""

from __future__ import annotations

from typing import Any

import pytest

from scorekeeper.core.retrieval.embed import Embedder, EmbedError, OpenAIEmbedder
from scorekeeper.db.models import EMBEDDING_DIMENSIONS


class _FakeEmbeddings:
    """Records every ``create`` call and returns vectors of ``dimensions`` floats.

    Defaults to the column's own width so the fake follows ``EMBEDDING_DIMENSIONS``
    instead of pinning a number these tests would have to chase on every change.
    """

    def __init__(
        self, *, dimensions: int = EMBEDDING_DIMENSIONS, exc: Exception | None = None
    ) -> None:
        self.dimensions = dimensions
        self.exc = exc
        self.batches: list[list[str]] = []

    def create(self, *, model: str, input: list[str]) -> Any:
        if self.exc is not None:
            raise self.exc
        self.batches.append(list(input))
        # Each vector encodes its own text's position so order is checkable.
        data = [
            type("Item", (), {"embedding": [float(len(text))] * self.dimensions})()
            for text in input
        ]
        return type("Response", (), {"data": data})()


class _FakeClient:
    def __init__(self, embeddings: _FakeEmbeddings) -> None:
        self.embeddings = embeddings


def _embedder(fake: _FakeEmbeddings, **kwargs: Any) -> OpenAIEmbedder:
    return OpenAIEmbedder(client=_FakeClient(fake), api_key="k", **kwargs)


def test_conforms_to_protocol() -> None:
    assert isinstance(_embedder(_FakeEmbeddings()), Embedder)


def test_splits_into_batches_and_preserves_order() -> None:
    fake = _FakeEmbeddings()
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    vectors = _embedder(fake, batch_size=2).embed(texts)
    assert fake.batches == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    # One vector per input, in input order across the batch boundaries.
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_no_texts_makes_no_call() -> None:
    fake = _FakeEmbeddings()
    assert _embedder(fake).embed([]) == []
    assert fake.batches == []


def test_wrong_width_is_rejected_before_it_reaches_the_column() -> None:
    # Any width but the column's is refused, whatever the column's happens to be.
    other = EMBEDDING_DIMENSIONS + 1
    fake = _FakeEmbeddings(dimensions=other)
    with pytest.raises(EmbedError, match=str(other)):
        _embedder(fake).embed(["a"])


def test_sdk_failure_is_wrapped() -> None:
    fake = _FakeEmbeddings(exc=RuntimeError("429 rate limited"))
    with pytest.raises(EmbedError, match="rate limited"):
        _embedder(fake).embed(["a"])


def test_no_api_key_raises_rather_than_building_a_client() -> None:
    embedder = OpenAIEmbedder(api_key="", model="m")
    embedder._api_key = ""  # also ignore whatever the environment configured
    with pytest.raises(EmbedError, match="openai_api_key"):
        embedder.embed(["a"])


def test_explicit_model_is_used() -> None:
    fake = _FakeEmbeddings()
    calls: list[str] = []
    original = fake.create

    def spy(*, model: str, input: list[str]):
        calls.append(model)
        return original(model=model, input=input)

    fake.create = spy  # type: ignore[method-assign]
    _embedder(fake, model="text-embedding-3-large").embed(["a"])
    assert calls == ["text-embedding-3-large"]
