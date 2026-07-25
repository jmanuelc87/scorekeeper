"""Endpoint tests for ``/captures`` — the browser-capture twin of /evaluations."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from scorekeeper import tasks
from scorekeeper.api import app
from scorekeeper.core.services import ingestion


async def test_captures_endpoint_ingests_without_enqueue(monkeypatch) -> None:
    captured: dict = {}

    async def fake_ingest(platform, files, *, session=None):
        captured["platform"] = platform
        captured["files"] = files
        return "run-cap"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post(
            "/captures",
            json={
                "platform": "gemini",
                "use_case": "web_search",
                "conversations": [
                    {
                        "scenario_id": "esc-navegador",
                        "source_ref": "https://gemini.google.com/app/abc",
                        "messages": [
                            {"role": "user", "content": "¿Cuál es la capital?"},
                            {"role": "model", "content": "Madrid."},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-cap", "status": "ingerido"}
    assert "enqueued" not in captured  # ingestion is decoupled from starting
    assert captured["platform"] == "gemini"
    upload = captured["files"][0]
    assert upload.scenario_id == "esc-navegador"
    assert upload.use_case == "web_search"  # payload default, no per-conversation one
    assert upload.filename == "https://gemini.google.com/app/abc"
    assert upload.messages == [
        {"role": "user", "content": "¿Cuál es la capital?"},
        {"role": "model", "content": "Madrid."},
    ]
    # No file bytes exist, so the provenance hash covers the capture itself.
    assert json.loads(upload.content.decode()) == upload.messages


async def test_captures_endpoint_per_conversation_overrides(monkeypatch) -> None:
    captured: dict = {}
    async def fake_ingest(platform, files, **kw):
        captured.update(files=files)
        return "run-cap"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/captures",
            json={
                "platform": "claude",
                "conversations": [
                    {
                        "scenario_id": "esc1",
                        "platform": "copilot",
                        "use_case": "document_retrieval",
                        "messages": [{"role": "user", "content": "Hola"}],
                    }
                ],
            },
        )

    assert response.status_code == 202
    upload = captured["files"][0]
    assert upload.platform == "copilot"
    assert upload.use_case == "document_retrieval"
    # Without a source_ref the scenario id names the "file".
    assert upload.filename == "esc1"


async def test_captures_endpoint_rejects_contentless_conversation() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/captures",
            json={
                "platform": "claude",
                "conversations": [
                    {"scenario_id": "vacio", "messages": [{"role": "user", "content": "  "}]}
                ],
            },
        )

    assert response.status_code == 400
    assert "vacio" in response.json()["detail"]


async def test_captures_endpoint_requires_conversations() -> None:
    with TestClient(app) as client:
        response = client.post("/captures", json={"platform": "claude", "conversations": []})

    assert response.status_code == 422
