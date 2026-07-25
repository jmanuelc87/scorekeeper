"""Endpoint tests for ``/runs``."""

from __future__ import annotations


from fastapi.testclient import TestClient

from scorekeeper.api import app
from scorekeeper.core.services import read_models


async def test_runs_endpoint_forwards_filters_and_returns_list(monkeypatch) -> None:
    captured: dict = {}
    runs = [{"run_id": "run-1", "status": "completado", "platforms": []}]

    async def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return runs

    monkeypatch.setattr(read_models, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get(
            "/runs",
            params={
                "run_id": "run-1",
                "platform": "claude",
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
                "granularity": "metric_scores",
            },
        )

    assert response.status_code == 200
    assert response.json() == runs
    # Every query param is forwarded to retrieve_runs.
    assert captured == {
        "run_id": "run-1",
        "platform": "claude",
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "granularity": "metric_scores",
    }


async def test_runs_endpoint_defaults_granularity_and_empty_result(monkeypatch) -> None:
    captured: dict = {}

    async def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(read_models, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get("/runs")

    assert response.status_code == 200
    assert response.json() == []
    # No filters given; granularity defaults to scenario_results.
    assert captured == {
        "run_id": None,
        "platform": None,
        "start_date": None,
        "end_date": None,
        "granularity": "scenario_results",
    }


async def test_runs_endpoint_invalid_input_400(monkeypatch) -> None:
    async def fake_retrieve(**kwargs):
        raise ValueError("Granularidad 'nope' inválida")

    monkeypatch.setattr(read_models, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get("/runs", params={"granularity": "nope"})

    assert response.status_code == 400
