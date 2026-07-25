"""Endpoint tests for ``/turns/{turn_id}`` traces and token usage."""

from __future__ import annotations


from fastapi.testclient import TestClient

from scorekeeper.api import app
from scorekeeper.core.services import read_models


async def _async_none(*args: object, **kwargs: object) -> None:
    """Awaitable stand-in for a service function that returns ``None`` (unknown id)."""
    return None


async def test_turn_traces_endpoint_forwards_and_returns(monkeypatch) -> None:
    captured: dict = {}
    payload = [{"metric_name": "utilidad", "trace": {"steps": []}}]

    async def fake(turn_id, **kwargs):
        captured["turn_id"] = turn_id
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(read_models, "retrieve_turn_traces", fake)

    with TestClient(app) as client:
        response = client.get("/turns/abc/traces", params={"provenance": "false"})

    assert response.status_code == 200
    assert response.json() == payload
    assert captured == {"turn_id": "abc", "include_provenance": False}


async def test_turn_traces_endpoint_unknown_turn_404(monkeypatch) -> None:
    monkeypatch.setattr(read_models, "retrieve_turn_traces", _async_none)

    with TestClient(app) as client:
        response = client.get("/turns/nope/traces")

    assert response.status_code == 404


async def test_turn_token_usage_endpoint_forwards_and_returns(monkeypatch) -> None:
    captured: dict = {}
    payload = {
        "turn_id": "abc",
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
    }

    async def fake(turn_id, **kwargs):
        captured["turn_id"] = turn_id
        return payload

    monkeypatch.setattr(read_models, "retrieve_turn_token_usage", fake)

    with TestClient(app) as client:
        response = client.get("/turns/abc/token-usage")

    assert response.status_code == 200
    assert response.json() == payload
    assert captured == {"turn_id": "abc"}


async def test_turn_token_usage_endpoint_unknown_turn_404(monkeypatch) -> None:
    monkeypatch.setattr(read_models, "retrieve_turn_token_usage", _async_none)

    with TestClient(app) as client:
        response = client.get("/turns/nope/token-usage")

    assert response.status_code == 404
