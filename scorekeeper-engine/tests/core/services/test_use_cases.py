"""Tests for ``core.services.use_cases`` — composing the metric set of a use case.

Metric selection is user data: no metric declares a use case, so these exercise the
only writer of ``use_case_metrics``. The registry is isolated to a single fake metric
so "unknown metric" is a real condition rather than an accident of the catalog.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.metrics.selection import DEFAULT_USE_CASE, resolve
from scorekeeper.core.services import use_cases as service


class Utilidad(Metric):
    name: ClassVar[str] = "utilidad"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


class Correccion(Utilidad):
    name: ClassVar[str] = "correccion"


@pytest.fixture
def registry():
    """Isolate the registry to two fake metrics, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    MetricRegistry.add(Correccion)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


def test_list_metrics_returns_the_registry_catalog(registry) -> None:
    catalog = service.list_metrics()

    assert [entry["name"] for entry in catalog] == ["correccion", "utilidad"]
    assert catalog[0]["category"] == "rag"
    assert catalog[0]["weight"] == 1.0
    assert catalog[0]["rubric_version"] == "v1"


async def test_create_links_the_requested_metrics(session: AsyncSession, registry) -> None:
    created = await service.create_use_case("soporte", ["utilidad"], session=session)

    assert created["name"] == "soporte"
    assert created["metrics"] == ["utilidad"]
    # The stored set is what the runner will score with.
    metrics = await resolve(session, uuid.UUID(created["id"]))
    assert [metric.name for metric in metrics] == ["utilidad"]


async def test_create_also_seeds_the_catalog_and_default(
    session: AsyncSession, registry
) -> None:
    await service.create_use_case("soporte", ["utilidad"], session=session)

    # sync_metrics runs first, so a use case can be composed before any ingest.
    listed = {entry["name"] for entry in await service.list_use_cases(session=session)}
    assert listed == {"soporte", DEFAULT_USE_CASE}


async def test_list_reports_each_use_case_with_its_metrics(
    session: AsyncSession, registry
) -> None:
    await service.create_use_case("soporte", ["utilidad", "correccion"], session=session)

    listed = {entry["name"]: entry["metrics"] for entry in await service.list_use_cases(session=session)}

    assert listed["soporte"] == ["correccion", "utilidad"]
    # "default" scores the whole registry, so an upload that picks no set still gets a
    # full evaluation.
    assert listed[DEFAULT_USE_CASE] == ["correccion", "utilidad"]


async def test_create_rejects_a_duplicate_name(session: AsyncSession, registry) -> None:
    await service.create_use_case("soporte", ["utilidad"], session=session)

    with pytest.raises(service.UseCaseConflictError, match="ya existe"):
        await service.create_use_case("soporte", ["correccion"], session=session)


async def test_create_rejects_an_unregistered_metric(
    session: AsyncSession, registry
) -> None:
    with pytest.raises(service.UseCaseValidationError, match="Métrica\\(s\\) desconocida\\(s\\)"):
        await service.create_use_case("soporte", ["fantasma"], session=session)


async def test_create_rejects_an_empty_metric_set(session: AsyncSession, registry) -> None:
    with pytest.raises(service.UseCaseValidationError, match="al menos una métrica"):
        await service.create_use_case("soporte", [], session=session)


async def test_create_rejects_a_blank_name(session: AsyncSession, registry) -> None:
    with pytest.raises(service.UseCaseValidationError, match="no puede estar vacío"):
        await service.create_use_case("   ", ["utilidad"], session=session)


async def test_create_deduplicates_repeated_metrics(
    session: AsyncSession, registry
) -> None:
    # A repeated name must not trip the (use_case_id, metric_id) unique constraint.
    created = await service.create_use_case(
        "soporte", ["utilidad", "utilidad"], session=session
    )

    assert created["metrics"] == ["utilidad"]
