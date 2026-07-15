"""Shared test fixtures for the metric taxonomy — no live LLM, no real DB.

The catalog ships empty (concrete metrics are project-specific), so these tests
define their own demo metrics and register them into an isolated registry via the
``registered_metrics`` fixture, which restores global state afterward.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scorekeeper.database import Base
from scorekeeper.metrics.base import (
    MetricResult,
    MultiStepMetric,
    SingleRubricMetric,
    StepTrace,
    TurnView,
)
from scorekeeper.metrics.category import MetricCategory
from scorekeeper.metrics.judge import Judge, JudgeVerdict
from scorekeeper.metrics.registry import MetricRegistry
from scorekeeper.metrics.scale import Boolean, Likert, Unit


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


# --- Demo metrics (test-only; the real catalog is intentionally empty) --------


class Claims(BaseModel):
    afirmaciones: list[str] = []
    summary: str = ""


class DemoCorreccion(SingleRubricMetric):
    name = "correccion"
    category = MetricCategory.CALIDAD
    scale = Likert()  # 1-5
    weight = 2.0
    scenarios = ("soporte_tecnico", "ventas")
    rubric = "Evalúa la corrección (1-5): {prompt} {response}"


class DemoUtilidad(SingleRubricMetric):
    name = "utilidad"
    category = MetricCategory.CALIDAD
    scale = Likert()
    weight = 1.0
    scenarios = ("soporte_tecnico", "ventas")
    rubric = "Evalúa la utilidad (1-5): {prompt} {response}"


class DemoTono(SingleRubricMetric):
    name = "tono"
    category = MetricCategory.COMUNICACION
    scale = Likert()
    weight = 1.0
    scenarios = ("ventas",)
    rubric = "Evalúa el tono (1-5): {prompt} {response}"


class DemoSeguridad(MultiStepMetric):
    name = "seguridad_factual"
    category = MetricCategory.SEGURIDAD
    scale = Unit()  # 0-1
    weight = 3.0
    scenarios = ("soporte_tecnico",)

    def evaluate(self, turn: TurnView, judge: Judge) -> MetricResult:
        trace: list[StepTrace] = []
        extraction = judge.structured(instruction="extrae", turn=turn, schema=Claims)
        trace.append(StepTrace(label="Extracción de afirmaciones", detail=extraction.summary))
        verdicts = [
            judge.score(
                rubric=f"verifica: {afirmacion}",
                turn=turn,
                scale=Boolean(),
                rubric_version=self.rubric_version,
            )
            for afirmacion in extraction.afirmaciones
        ]
        for afirmacion, verdict in zip(extraction.afirmaciones, verdicts, strict=True):
            trace.append(StepTrace(label=f"Verificación: {afirmacion}", detail=verdict.justification))
        raw = sum(v.score for v in verdicts) / len(verdicts) if verdicts else 1.0
        return MetricResult(
            metric_name=self.name,
            raw_score=raw,
            normalized_score=self.normalize(raw),
            justification=self.render_justification(trace),
            judge_model=verdicts[0].model if verdicts else None,
            rubric_version=self.rubric_version,
            trace=trace,
        )


DEMO_METRICS = [DemoCorreccion, DemoUtilidad, DemoTono, DemoSeguridad]


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def registered_metrics():
    """Register the demo metrics into an isolated registry, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    for metric_cls in DEMO_METRICS:
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
def make_extraction():
    """Factory for a Claims extraction result used by the multi-step demo metric."""

    def _make(afirmaciones: list[str], summary: str = "resumen") -> Claims:
        return Claims(afirmaciones=list(afirmaciones), summary=summary)

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
