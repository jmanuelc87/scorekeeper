"""HTTP-level tests for ``/use-cases`` and ``/metrics``.

Drives the real FastAPI stack against the shared in-memory SQLite DB (the root conftest
points ``SessionLocal`` at it), over ``httpx.AsyncClient`` rather than ``TestClient``
for the same reason as the auth-provider tests: these routes touch the database, and
TestClient would drive the app on a different event loop than the aiosqlite engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

import httpx
import pytest
from httpx import ASGITransport

from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.registry import MetricRegistry
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.main import app


class Utilidad(Metric):
    name: ClassVar[str] = "utilidad"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
def registry():
    """Isolate the registry to one fake metric, then restore."""
    saved = MetricRegistry.all()
    MetricRegistry.clear()
    MetricRegistry.add(Utilidad)
    try:
        yield
    finally:
        MetricRegistry.clear()
        for metric_cls in saved:
            MetricRegistry.add(metric_cls)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_get_metrics_lists_the_catalog(client: httpx.AsyncClient, registry) -> None:
    response = await client.get("/api/v1/metrics")

    assert response.status_code == 200
    assert response.json() == [
        {"name": "utilidad", "category": "rag", "weight": 1.0, "rubric_version": "v1"}
    ]


async def test_post_creates_a_use_case(client: httpx.AsyncClient, registry) -> None:
    response = await client.post(
        "/api/v1/use-cases", json={"name": "soporte", "metrics": ["utilidad"]}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "soporte"
    assert body["metrics"] == ["utilidad"]
    assert body["id"]


async def test_get_lists_created_use_cases(client: httpx.AsyncClient, registry) -> None:
    await client.post("/api/v1/use-cases", json={"name": "soporte", "metrics": ["utilidad"]})

    response = await client.get("/api/v1/use-cases")

    assert response.status_code == 200
    listed = {entry["name"]: entry["metrics"] for entry in response.json()}
    assert listed["soporte"] == ["utilidad"]
    # "default" scores every registered metric.
    assert listed["default"] == ["utilidad"]


async def test_post_duplicate_name_is_409(client: httpx.AsyncClient, registry) -> None:
    await client.post("/api/v1/use-cases", json={"name": "soporte", "metrics": ["utilidad"]})

    response = await client.post(
        "/api/v1/use-cases", json={"name": "soporte", "metrics": ["utilidad"]}
    )

    assert response.status_code == 409
    assert "ya existe" in response.json()["detail"]


async def test_post_unknown_metric_is_422(client: httpx.AsyncClient, registry) -> None:
    response = await client.post(
        "/api/v1/use-cases", json={"name": "soporte", "metrics": ["fantasma"]}
    )

    assert response.status_code == 422
    assert "desconocida" in response.json()["detail"]
