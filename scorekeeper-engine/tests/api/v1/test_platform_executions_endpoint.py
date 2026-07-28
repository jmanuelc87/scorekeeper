"""Endpoint tests for ``/platform-executions``."""

from __future__ import annotations


from fastapi.testclient import TestClient

from scorekeeper.main import app
from scorekeeper.core.services import read_models

# The endpoint declares a response_model, so a stub must carry every field —
# an incomplete dict is a 500 ResponseValidationError, not a 200.
_EXECUTION = {
    "id": "3f1b6c2e-0000-4000-8000-000000000001",
    "run_id": "3f1b6c2e-0000-4000-8000-000000000002",
    "platform": "claude",
    "started_at": "2026-07-10T12:00:00+00:00",
    "finished_at": "2026-07-10T12:05:00+00:00",
    "average_score": 0.81,
    "scenarios": 4,
    "status_breakdown": {"completado": 4},
}


async def test_platform_executions_endpoint_forwards_filters_and_returns_list(
    monkeypatch,
) -> None:
    captured: dict = {}

    async def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return [_EXECUTION]

    monkeypatch.setattr(read_models, "retrieve_platform_executions", fake_retrieve)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform-executions",
            params={
                "platform": "claude",
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
            },
        )

    assert response.status_code == 200
    # The response_model neither drops nor reshapes a field.
    assert response.json() == [_EXECUTION]
    # Every query param is forwarded — and nothing else (no run_id/granularity).
    assert captured == {
        "platform": "claude",
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
    }


async def test_platform_executions_endpoint_without_filters(monkeypatch) -> None:
    captured: dict = {}

    async def fake_retrieve(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(read_models, "retrieve_platform_executions", fake_retrieve)

    with TestClient(app) as client:
        response = client.get("/api/v1/platform-executions")

    assert response.status_code == 200
    assert response.json() == []
    # Every filter is optional and defaults to None.
    assert captured == {"platform": None, "start_date": None, "end_date": None}


async def test_platform_executions_endpoint_invalid_date_400(monkeypatch) -> None:
    message = "start_date 'ayer' inválido; use un formato ISO-8601 (YYYY-MM-DD)."

    async def fake_retrieve(**kwargs):
        raise ValueError(message)

    monkeypatch.setattr(read_models, "retrieve_platform_executions", fake_retrieve)

    with TestClient(app) as client:
        response = client.get("/api/v1/platform-executions", params={"start_date": "ayer"})

    assert response.status_code == 400
    # The service's message reaches the client verbatim.
    assert response.json()["detail"] == message
