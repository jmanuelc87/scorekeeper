"""Shared test fixtures for the metric taxonomy — no live LLM, no real DB.

Tests exercise the taxonomy machinery (base classes, registry, selection,
rollup) through the real catalog metrics. The ``registered_metrics`` fixture
loads those into an isolated registry and restores global state afterward.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scorekeeper.database import Base
from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.catalog.faithfulness import (
    FaithfulnessDeepeval,
    FaithfulnessRagas,
)
from scorekeeper.metrics.catalog.hallucination import Hallucination
from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry


class StubJudge:
    """A Judge that returns scripted verdicts/extractions and records its calls."""

    def __init__(
        self,
        verdicts: list[JudgeVerdict] | None = None,
        extractions: list[BaseModel] | None = None,
        embeddings: dict[str, list[float]] | None = None,
        model: str | None = None,
    ) -> None:
        self._verdicts = list(verdicts or [])
        self._extractions = list(extractions or [])
        # Map text -> vector; embed() returns one vector per requested text.
        self._embeddings = dict(embeddings or {})
        self.model = model
        self.calls: list[tuple[str, str | None]] = []

    def score(self, *, rubric, turn, scale, rubric_version=None) -> JudgeVerdict:
        self.calls.append(("score", rubric_version))
        return self._verdicts.pop(0)

    def structured(self, *, instruction, turn, schema):
        self.calls.append(("structured", schema.__name__))
        return self._extractions.pop(0)

    def embed(self, *, texts):
        self.calls.append(("embed", str(len(texts))))
        # Unknown texts embed to a zero vector, keeping the stub total.
        return [self._embeddings.get(text, [0.0, 0.0]) for text in texts]


# The concrete catalog metrics under test.
CATALOG_METRICS = [FaithfulnessRagas, FaithfulnessDeepeval, Hallucination]


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def registered_metrics():
    """Register the catalog metrics into an isolated registry, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    for metric_cls in CATALOG_METRICS:
        MetricRegistry.add(metric_cls)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


@pytest.fixture
def make_judge():
    """Factory for a StubJudge with scripted verdicts/extractions."""

    def _make(
        verdicts: list[JudgeVerdict] | None = None,
        extractions: list[BaseModel] | None = None,
        embeddings: dict[str, list[float]] | None = None,
        model: str | None = None,
    ) -> StubJudge:
        return StubJudge(
            verdicts=verdicts, extractions=extractions, embeddings=embeddings, model=model
        )

    return _make


@pytest.fixture
def turn() -> TurnView:
    return TurnView(prompt="¿Cómo reinicio el router?", response="Mantén pulsado 10s.")


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
