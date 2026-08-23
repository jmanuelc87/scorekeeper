"""Endpoint tests for ``/captures`` — the browser-capture twin of /evaluations."""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from scorekeeper import tasks
from scorekeeper.main import app
from scorekeeper.core.services import ingestion
from scorekeeper.db.models import BenchmarkRun, PlatformExecution, ScenarioResult


async def test_captures_endpoint_ingests_without_enqueue(monkeypatch) -> None:
    captured: dict = {}

    async def fake_ingest(platform, files, **kw):
        captured["platform"] = platform
        captured["reuse_scenarios"] = kw.get("reuse_scenarios")
        captured["files"] = files
        return "run-cap"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)
    monkeypatch.setattr(tasks, "enqueue_run", lambda run_id: captured.update(enqueued=run_id))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/captures",
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
    # The capture path extends an already-ingested scenario; /evaluations does not.
    assert captured["reuse_scenarios"] is True
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
            "/api/v1/captures",
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


async def test_captures_endpoint_carries_the_detected_model(monkeypatch) -> None:
    """``model_name`` reaches the upload; a blank or absent one means unknown."""
    captured: dict = {}

    async def fake_ingest(platform, files, **kw):
        captured.update(files=files)
        return "run-cap"

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/captures",
            json={
                "platform": "claude",
                "conversations": [
                    {
                        "scenario_id": "esc-detectado",
                        "model_name": "Claude Opus 4.5",
                        "messages": [{"role": "user", "content": "Hola"}],
                    },
                    # The popup's field cleared by hand.
                    {
                        "scenario_id": "esc-vacio",
                        "model_name": "   ",
                        "messages": [{"role": "user", "content": "Hola"}],
                    },
                    # Nothing detected and nothing typed: the key never arrives.
                    {
                        "scenario_id": "esc-omitido",
                        "messages": [{"role": "user", "content": "Hola"}],
                    },
                ],
            },
        )

    assert response.status_code == 202
    assert [upload.model_name for upload in captured["files"]] == [
        "Claude Opus 4.5",
        None,
        None,
    ]


async def test_captures_endpoint_rejects_contentless_conversation() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/captures",
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
        response = client.post("/api/v1/captures", json={"platform": "claude", "conversations": []})

    assert response.status_code == 422


async def test_captures_endpoint_unknown_use_case_422(monkeypatch) -> None:
    async def fake_ingest(platform, files, **kw):
        raise ingestion.UnknownUseCaseError(
            "Caso(s) de uso desconocido(s): inexistente. Créalo con POST /use-cases."
        )

    monkeypatch.setattr(ingestion, "ingest_evaluation", fake_ingest)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/captures",
            json={
                "platform": "gemini",
                "use_case": "inexistente",
                "conversations": [
                    {
                        "scenario_id": "esc-1",
                        "messages": [{"role": "user", "content": "hola"}],
                    }
                ],
            },
        )

    # 422, not the 400 a malformed capture gets: UnknownUseCaseError subclasses
    # ValueError, so this asserts the handler's except-arm ordering.
    assert response.status_code == 422
    assert "Caso(s) de uso desconocido(s): inexistente" in response.json()["detail"]


def _capture(scenario_id: str, platform: str, *, use_case: str | None = None) -> dict:
    """One-conversation ``/captures`` payload for ``scenario_id`` on ``platform``."""
    conversation: dict = {
        "scenario_id": scenario_id,
        "messages": [
            {"role": "user", "content": "¿Cuál es la capital?"},
            {"role": "model", "content": "Madrid."},
        ],
    }
    if use_case is not None:
        conversation["use_case"] = use_case
    return {"platform": platform, "conversations": [conversation]}


async def test_captures_group_under_the_scenario_already_ingested(
    session, compose_use_case
) -> None:
    """One capture per platform builds a single scenario, not three runs."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_capture("esc-01", "copilot"))
        second = client.post("/api/v1/captures", json=_capture("esc-01", "gemini"))
        third = client.post("/api/v1/captures", json=_capture("esc-01", "claude"))

    assert [r.status_code for r in (first, second, third)] == [202, 202, 202]
    # Every call answers with the run the first one opened.
    run_id = first.json()["run_id"]
    assert second.json()["run_id"] == run_id
    assert third.json()["run_id"] == run_id

    runs = (await session.execute(select(func.count()).select_from(BenchmarkRun))).scalar_one()
    assert runs == 1
    scenario = (
        await session.scalars(
            select(ScenarioResult).options(selectinload(ScenarioResult.platform_executions))
        )
    ).one()
    assert scenario.scenario_id == "esc-01"
    assert [pe.platform for pe in scenario.platform_executions] == ["claude", "copilot", "gemini"]


async def test_captures_open_a_new_run_once_the_previous_one_started(
    session, compose_use_case
) -> None:
    """A started run keeps its turns: appending unscored ones would skew its roll-ups."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_capture("esc-01", "copilot"))
        run = await session.get(BenchmarkRun, uuid.UUID(first.json()["run_id"]))
        run.status = "en_cola"
        await session.commit()
        second = client.post("/api/v1/captures", json=_capture("esc-01", "gemini"))

    assert second.status_code == 202
    assert second.json()["run_id"] != first.json()["run_id"]
    runs = (await session.execute(select(func.count()).select_from(BenchmarkRun))).scalar_one()
    assert runs == 2


async def test_captures_open_a_new_run_once_the_scenario_left_pending(
    session, compose_use_case
) -> None:
    """A scored scenario keeps the executions it was scored on.

    The run status is not the only guard: a scenario that has rolled up past ``pending``
    must not gain an execution, because its roll-up would then describe a set of
    executions that no longer exists. Here the run is left at ``ingerido`` on purpose, so
    only the scenario status can reject the second capture.
    """
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_capture("esc-01", "copilot"))
        scenario = (await session.scalars(select(ScenarioResult))).one()
        scenario.status = "completado"
        await session.commit()
        second = client.post("/api/v1/captures", json=_capture("esc-01", "gemini"))

    assert second.status_code == 202
    assert second.json()["run_id"] != first.json()["run_id"]
    runs = (await session.execute(select(func.count()).select_from(BenchmarkRun))).scalar_one()
    assert runs == 2
    # The scored scenario is untouched; the new capture opened one of its own.
    scored = (
        await session.scalars(
            select(ScenarioResult)
            .where(ScenarioResult.status == "completado")
            .options(selectinload(ScenarioResult.platform_executions))
        )
    ).one()
    assert [pe.platform for pe in scored.platform_executions] == ["copilot"]


async def test_captures_reject_a_reused_scenario_with_another_use_case(
    session, compose_use_case
) -> None:
    await compose_use_case(["precision"])
    await compose_use_case(["claridad"], use_case="otro")

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_capture("esc-01", "copilot"))
        second = client.post("/api/v1/captures", json=_capture("esc-01", "gemini", use_case="otro"))

    assert first.status_code == 202
    assert second.status_code == 400
    assert "esc-01" in second.json()["detail"]
    # Rejected before anything was persisted: the first capture is untouched.
    executions = (
        await session.execute(select(func.count()).select_from(PlatformExecution))
    ).scalar_one()
    assert executions == 1


def _labelled(scenario_id: str, platform: str, label: str) -> dict:
    """``_capture`` plus the batch label the run is grouped under."""
    return {**_capture(scenario_id, platform), "run_label": label}


async def test_captures_sharing_a_label_group_under_one_run(session, compose_use_case) -> None:
    """Different scenarios, one label: one run holding all three scenarios."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        responses = [
            client.post("/api/v1/captures", json=_labelled(scenario_id, "copilot", "lote-agosto"))
            for scenario_id in ("esc-01", "esc-02", "esc-03")
        ]

    assert [r.status_code for r in responses] == [202, 202, 202]
    run_ids = {r.json()["run_id"] for r in responses}
    assert len(run_ids) == 1

    run = (await session.scalars(select(BenchmarkRun))).one()
    assert str(run.id) == run_ids.pop()
    assert run.label == "lote-agosto"
    # Asserted on persisted rows: a scenario hung off an already-persistent run is not
    # cascaded into the session by the backref, so it could return the right run id and
    # still never be written.
    scenarios = (
        await session.scalars(
            select(ScenarioResult)
            .where(ScenarioResult.run_id == run.id)
            .order_by(ScenarioResult.scenario_id)
        )
    ).all()
    assert [s.scenario_id for s in scenarios] == ["esc-01", "esc-02", "esc-03"]
    executions = (
        await session.execute(select(func.count()).select_from(PlatformExecution))
    ).scalar_one()
    assert executions == 3


async def test_captures_repeating_a_scenario_under_a_label_extend_it(
    session, compose_use_case
) -> None:
    """The scenario-level reuse still applies inside the batch."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_labelled("esc-01", "copilot", "lote"))
        second = client.post("/api/v1/captures", json=_labelled("esc-01", "gemini", "lote"))

    assert first.json()["run_id"] == second.json()["run_id"]
    scenario = (
        await session.scalars(
            select(ScenarioResult).options(selectinload(ScenarioResult.platform_executions))
        )
    ).one()
    assert [pe.platform for pe in scenario.platform_executions] == ["copilot", "gemini"]


async def test_captures_open_a_new_run_once_the_labelled_one_started(
    session, compose_use_case
) -> None:
    """A started batch is closed: the label opens a fresh run instead of reopening it."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_labelled("esc-01", "copilot", "lote"))
        run = await session.get(BenchmarkRun, uuid.UUID(first.json()["run_id"]))
        run.status = "en_cola"
        await session.commit()
        second = client.post("/api/v1/captures", json=_labelled("esc-02", "copilot", "lote"))

    assert second.status_code == 202
    assert second.json()["run_id"] != first.json()["run_id"]
    labels = (await session.scalars(select(BenchmarkRun.label))).all()
    assert list(labels) == ["lote", "lote"]


async def test_captures_with_different_labels_stay_apart(session, compose_use_case) -> None:
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_labelled("esc-01", "copilot", "lote-a"))
        second = client.post("/api/v1/captures", json=_labelled("esc-02", "copilot", "lote-b"))

    assert first.json()["run_id"] != second.json()["run_id"]
    runs = (
        await session.scalars(select(BenchmarkRun).order_by(BenchmarkRun.label))
    ).all()
    assert [r.label for r in runs] == ["lote-a", "lote-b"]


async def test_a_labelled_capture_never_joins_an_unlabelled_scenario(
    session, compose_use_case
) -> None:
    """The scenario lookup is scoped to the batch, so it cannot cross into another run."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        loose = client.post("/api/v1/captures", json=_capture("esc-01", "copilot"))
        batched = client.post("/api/v1/captures", json=_labelled("esc-01", "gemini", "lote"))

    assert batched.json()["run_id"] != loose.json()["run_id"]
    runs = (await session.execute(select(func.count()).select_from(BenchmarkRun))).scalar_one()
    assert runs == 2
    scenarios = (
        await session.scalars(
            select(ScenarioResult).options(selectinload(ScenarioResult.platform_executions))
        )
    ).all()
    assert [[pe.platform for pe in s.platform_executions] for s in scenarios] == [
        ["copilot"],
        ["gemini"],
    ]


async def test_captures_without_a_label_keep_the_legacy_grouping(
    session, compose_use_case
) -> None:
    """No label: two scenarios, two runs — the pre-batch behaviour, unchanged."""
    await compose_use_case(["precision"])

    with TestClient(app) as client:
        first = client.post("/api/v1/captures", json=_capture("esc-01", "copilot"))
        second = client.post("/api/v1/captures", json=_capture("esc-02", "copilot"))

    assert first.json()["run_id"] != second.json()["run_id"]
    labels = (await session.scalars(select(BenchmarkRun.label))).all()
    assert list(labels) == [None, None]
