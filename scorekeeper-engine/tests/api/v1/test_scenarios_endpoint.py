"""Endpoint tests for ``/scenarios/{scenario_id}/turns``."""

from __future__ import annotations


from fastapi.testclient import TestClient

from scorekeeper.main import app
from scorekeeper.core.services import read_models


async def _async_none(*args: object, **kwargs: object) -> None:
    """Awaitable stand-in for a service function that returns ``None`` (unknown id)."""
    return None


async def test_scenario_turns_endpoint_forwards_and_returns(monkeypatch) -> None:
    captured: dict = {}
    payload = [
        {
            "turn_id": "11111111-1111-1111-1111-111111111111",
            "turn_number": 1,
            "prompt": "hola",
            "response": "qué tal",
            "expected_output": None,
            "retrieved_context_source": None,
            "turn_score": 0.8,
            "metric_scores": [
                {
                    "metric_name": "utilidad",
                    "score": 0.8,
                    "judge_model": "judge-test",
                    "rubric_version": "v1",
                }
            ],
        }
    ]

    async def fake(scenario_id):
        captured["scenario_id"] = scenario_id
        return payload

    monkeypatch.setattr(read_models, "retrieve_scenario_turns", fake)

    with TestClient(app) as client:
        response = client.get("/api/v1/scenarios/abc/turns")

    assert response.status_code == 200
    assert response.json() == payload
    assert captured == {"scenario_id": "abc"}


async def test_scenario_turns_endpoint_unknown_404(monkeypatch) -> None:
    monkeypatch.setattr(read_models, "retrieve_scenario_turns", _async_none)

    with TestClient(app) as client:
        response = client.get("/api/v1/scenarios/nope/turns")

    assert response.status_code == 404
