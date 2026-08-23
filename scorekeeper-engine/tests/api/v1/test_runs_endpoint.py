"""Endpoint tests for ``/runs``."""

from __future__ import annotations


from fastapi.testclient import TestClient

from scorekeeper.main import app
from scorekeeper.core.services import read_models


async def _async_none(*args, **kwargs) -> None:
    return None


async def test_runs_endpoint_forwards_filters_and_returns_list(monkeypatch) -> None:
    captured: dict = {}
    runs = [{"run_id": "run-1", "status": "completado", "platforms": []}]

    async def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return runs

    monkeypatch.setattr(read_models, "retrieve_runs", fake_retrieve)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/runs",
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
        response = client.get("/api/v1/runs")

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
        response = client.get("/api/v1/runs", params={"granularity": "nope"})

    assert response.status_code == 400


def _scenario_payload() -> dict:
    return {
        "id": "3f0ac1d2-0000-4000-8000-000000000001",
        "scenario_id": "esc1",
        "use_case": "rag_completo",
        "status": "completado",
        "platform_executions": [
            {
                "id": "3f0ac1d2-0000-4000-8000-000000000002",
                "platform": "claude",
                "model_name": None,
                "status": "completado",
                "average_score": 0.81,
                "started_at": "2026-07-10T12:00:00+00:00",
                "finished_at": "2026-07-10T12:05:00+00:00",
            }
        ],
    }


async def test_run_scenarios_endpoint_forwards_filters_and_returns_list(monkeypatch) -> None:
    captured: dict = {}
    payload = [_scenario_payload()]

    async def fake(run_id, **kwargs):
        captured["run_id"] = run_id
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(read_models, "retrieve_run_scenarios", fake)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/runs/b1f2c3d4-0000-4000-8000-000000000000/scenarios",
            params={"platform": "claude", "status": "completado"},
        )

    assert response.status_code == 200
    assert response.json() == payload
    assert captured == {
        "run_id": "b1f2c3d4-0000-4000-8000-000000000000",
        "platform": "claude",
        "status": "completado",
    }


async def test_run_scenarios_endpoint_defaults_filters_to_none(monkeypatch) -> None:
    captured: dict = {}

    async def fake(run_id, **kwargs):
        captured["run_id"] = run_id
        captured.update(kwargs)
        return []

    monkeypatch.setattr(read_models, "retrieve_run_scenarios", fake)

    with TestClient(app) as client:
        response = client.get("/api/v1/runs/run-1/scenarios")

    assert response.status_code == 200
    # A known run no scenario matches is an empty list, not a 404.
    assert response.json() == []
    assert captured == {"run_id": "run-1", "platform": None, "status": None}


async def test_run_scenarios_endpoint_unknown_run_404(monkeypatch) -> None:
    monkeypatch.setattr(read_models, "retrieve_run_scenarios", _async_none)

    with TestClient(app) as client:
        response = client.get("/api/v1/runs/no-existe/scenarios")

    assert response.status_code == 404


async def test_run_scenarios_endpoint_emits_exactly_the_rollup_fields(monkeypatch) -> None:
    """The response model stops at the scenario rollup — extras are dropped."""

    async def fake(run_id, **kwargs):
        payload = _scenario_payload()
        payload["platform_executions"][0]["turns"] = [{"turn_number": 1}]
        return [payload]

    monkeypatch.setattr(read_models, "retrieve_run_scenarios", fake)

    with TestClient(app) as client:
        response = client.get("/api/v1/runs/b1f2/scenarios")

    assert response.status_code == 200
    body = response.json()[0]
    assert set(body) == {
        "id",
        "scenario_id",
        "use_case",
        "status",
        "platform_executions",
    }
    # The nested execution stops at its rollup too — the turns are dropped.
    assert set(body["platform_executions"][0]) == {
        "id",
        "platform",
        "model_name",
        "status",
        "average_score",
        "started_at",
        "finished_at",
    }
