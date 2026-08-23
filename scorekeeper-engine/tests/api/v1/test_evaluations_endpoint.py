"""Endpoint tests for ``/evaluations`` — request parsing and error mapping."""

from __future__ import annotations

import json
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from scorekeeper import tasks
from scorekeeper.main import app
from scorekeeper.core.services import ingestion
from scorekeeper.core.services import runs as run_service


async def _async_none(*args: object, **kwargs: object) -> None:
    """Awaitable stand-in for a service function that returns ``None`` (unknown id)."""
    return None


def _xlsx_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _conversation_bytes() -> bytes:
    return _xlsx_bytes(
        ["turn", "role", "content"],
        [
            [1, "user", "hola"],
            [1, "model", "qué tal"],
            [2, "user", "adiós"],
            [2, "model", "hasta luego"],
        ],
    )


def _payload(**over) -> str:
    body = {"platform": "claude", "use_case": "default"}
    body.update(over)
    return json.dumps(body)


async def test_endpoint_ingests_without_enqueue_and_returns_run_id(monkeypatch) -> None:
    captured: dict = {}

    async def fake_ingest(platform, files, *, session=None):
        captured["platform"] = platform
        captured["files"] = files
        return "run-123"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={
                "payload": _payload(
                    files={
                        "esc1.xlsx": {
                            "scenario_id": "custom",
                            "use_case": "faithfulness_ragas",
                        }
                    }
                )
            },
        )

    # 202 Accepted with the run id; ingestion is decoupled, so nothing is enqueued yet.
    assert response.status_code == 202
    body = response.json()
    assert body == {"run_id": "run-123", "status": "ingerido"}
    assert "enqueued" not in captured
    # Per-file overrides applied; defaults elsewhere.
    assert captured["platform"] == "claude"
    upload = captured["files"][0]
    assert upload.scenario_id == "custom"
    assert upload.use_case == "faithfulness_ragas"
    assert upload.platform is None  # no per-file platform → falls back to payload


async def test_endpoint_per_file_platform_override(monkeypatch) -> None:
    captured: dict = {}

    async def fake_ingest(platform, files, *, session=None):
        captured["platform"] = platform
        captured["files"] = {u.filename: u for u in files}
        return "run-9"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[
                ("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream")),
                ("files", ("esc2.xlsx", _conversation_bytes(), "application/octet-stream")),
            ],
            data={"payload": _payload(files={"esc2.xlsx": {"platform": "gemini"}})},
        )

    assert response.status_code == 202
    # Payload platform is the fallback; only esc2 overrides it.
    assert captured["platform"] == "claude"
    assert captured["files"]["esc1.xlsx"].platform is None
    assert captured["files"]["esc2.xlsx"].platform == "gemini"


async def test_endpoint_defaults_scenario_id_to_stem(monkeypatch) -> None:
    captured: dict = {}
    async def fake_ingest(platform, files, **kw):
        captured.update(files=files)
        return "run-x"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: None)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": _payload()},
        )

    assert response.status_code == 202
    upload = captured["files"][0]
    assert upload.scenario_id == "esc1"  # stem of esc1.xlsx
    assert upload.use_case == "default"


async def test_start_endpoint_enqueues_and_returns_queued(monkeypatch) -> None:
    captured: dict = {}

    async def fake_start(run_id, **kw):
        return "en_cola"

    monkeypatch.setattr(run_service, "start_run", fake_start)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations/run-123/start")

    # 202 Accepted; the run flips to en_cola and the pipeline is enqueued now.
    assert response.status_code == 202
    assert response.json() == {"run_id": "run-123", "status": "en_cola"}
    assert captured["enqueued"] == "run-123"


async def test_start_endpoint_unknown_run_404(monkeypatch) -> None:
    captured: dict = {}

    async def fake_start(run_id, **kw):
        return None

    monkeypatch.setattr(run_service, "start_run", fake_start)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations/does-not-exist/start")

    assert response.status_code == 404
    assert "enqueued" not in captured  # nothing enqueued for an unknown run


async def test_start_endpoint_already_started_409(monkeypatch) -> None:
    captured: dict = {}

    async def fake_start(run_id, **kw):
        raise ValueError("El run ya fue iniciado.")

    monkeypatch.setattr(run_service, "start_run", fake_start)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations/run-123/start")

    assert response.status_code == 409
    assert "enqueued" not in captured  # a re-start never enqueues a second job


async def test_resume_endpoint_enqueues_and_returns_queued(monkeypatch) -> None:
    captured: dict = {}

    async def fake_resume(run_id, **kw):
        captured["resumed"] = run_id
        return "en_cola"

    monkeypatch.setattr(run_service, "resume_run", fake_resume)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations/run-123/resume")

    # 202 Accepted; the stopped run goes back to en_cola and its chain is enqueued again.
    assert response.status_code == 202
    assert response.json() == {"run_id": "run-123", "status": "en_cola"}
    assert captured["resumed"] == "run-123"
    assert captured["enqueued"] == "run-123"


async def test_resume_endpoint_unknown_run_404(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(run_service, "resume_run", _async_none)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations/does-not-exist/resume")

    assert response.status_code == 404
    assert "enqueued" not in captured  # nothing enqueued for an unknown run


async def test_resume_endpoint_not_resumable_409(monkeypatch) -> None:
    captured: dict = {}

    async def fake_resume(run_id, **kw):
        raise ValueError("El run no se puede reanudar desde el estado 'en_proceso'.")

    monkeypatch.setattr(run_service, "resume_run", fake_resume)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations/run-123/resume")

    assert response.status_code == 409
    assert "enqueued" not in captured  # a live chain never gets a second job


async def test_selection_endpoint_returns_updated_count(monkeypatch) -> None:
    captured: dict = {}

    async def fake_select(run_id, turn_ids, is_selected, **kw):
        captured.update(run_id=run_id, turn_ids=turn_ids, is_selected=is_selected)
        return len(turn_ids)

    monkeypatch.setattr(run_service, "set_turn_selection", fake_select)

    with TestClient(app) as client:
        response = client.patch(
            "/api/v1/evaluations/run-123/turns/selection",
            json={"turn_ids": ["a", "b"], "is_selected": True},
        )

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-123", "updated": 2}
    assert captured == {"run_id": "run-123", "turn_ids": ["a", "b"], "is_selected": True}


async def test_selection_endpoint_unknown_run_404(monkeypatch) -> None:
    async def fake_select(*a, **kw):
        return None

    monkeypatch.setattr(run_service, "set_turn_selection", fake_select)

    with TestClient(app) as client:
        response = client.patch(
            "/api/v1/evaluations/does-not-exist/turns/selection",
            json={"turn_ids": ["a"]},
        )

    assert response.status_code == 404


async def test_selection_endpoint_already_started_409(monkeypatch) -> None:
    async def fake_select(run_id, turn_ids, is_selected, **kw):
        raise ValueError("El run ya fue iniciado.")

    monkeypatch.setattr(run_service, "set_turn_selection", fake_select)

    with TestClient(app) as client:
        response = client.patch(
            "/api/v1/evaluations/run-123/turns/selection",
            json={"turn_ids": ["a"]},
        )

    assert response.status_code == 409


async def test_endpoint_get_returns_summary(monkeypatch) -> None:
    async def fake_summary(run_id, **kw):
        return {
            "run_id": run_id,
            "status": "en_proceso",
            "progress": {"done": 1, "total": 2, "ratio": 0.5},
            "platforms": [
                {
                    "platform": "claude",
                    "average_score": None,
                    "scenarios": 1,
                    "status_breakdown": {},
                }
            ],
        }

    monkeypatch.setattr(run_service, "get_run_summary", fake_summary)

    with TestClient(app) as client:
        response = client.get("/api/v1/evaluations/run-123")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run-123"
    assert body["status"] == "en_proceso"
    assert body["progress"] == {"done": 1, "total": 2, "ratio": 0.5}
    assert body["platforms"][0]["average_score"] is None


async def test_endpoint_get_unknown_run_404(monkeypatch) -> None:
    monkeypatch.setattr(run_service, "get_run_summary", _async_none)

    with TestClient(app) as client:
        response = client.get("/api/v1/evaluations/does-not-exist")

    assert response.status_code == 404


async def test_endpoint_rejects_invalid_payload() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": "no-es-json"},
        )
    assert response.status_code == 422


async def test_endpoint_rejects_non_xlsx() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.csv", b"data", "text/csv"))],
            data={"payload": _payload()},
        )
    assert response.status_code == 400


async def test_endpoint_rejects_empty_file() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.xlsx", b"", "application/octet-stream"))],
            data={"payload": _payload()},
        )
    assert response.status_code == 400


async def test_endpoint_maps_value_error_to_400(monkeypatch) -> None:
    async def fake_ingest(*args, **kwargs):
        raise ValueError("hoja inválida")

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": _payload()},
        )
    assert response.status_code == 400


async def test_endpoint_unknown_use_case_422(monkeypatch) -> None:
    async def fake_ingest(platform, files, *, session=None):
        raise ingestion.UnknownUseCaseError(
            "Caso(s) de uso desconocido(s): inexistente. Créalo con POST /use-cases."
        )

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            files=[("files", ("esc1.xlsx", _conversation_bytes(), "application/octet-stream"))],
            data={"payload": _payload(use_case="inexistente")},
        )

    # 422, not the 400 a malformed sheet gets: UnknownUseCaseError subclasses ValueError,
    # so this asserts the handler's except-arm ordering.
    assert response.status_code == 422
    assert "Caso(s) de uso desconocido(s): inexistente" in response.json()["detail"]
