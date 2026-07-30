"""Tests for the Celery seam: the two tasks, the two enqueue helpers, and the conf.

Deliberately *sync* tests. The Celery tasks are sync and drive the async chain through
their own ``asyncio.run()``, which cannot nest inside a running event loop.
"""

from __future__ import annotations

from scorekeeper import tasks
from scorekeeper.celery_app import celery_app
from scorekeeper.core.services import chain


def test_worker_lost_requeues_the_task() -> None:
    # acks_late alone lets Celery ack a job whose prefork child was killed; rejecting is
    # what actually re-delivers it so the run can resume forward.
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_run_chain_task_enqueues_the_first_turn(monkeypatch) -> None:
    async def _start(run_id: str) -> chain.NextTurn:
        assert run_id == "run-123"
        return chain.NextTurn("turno-1", 0.0)

    enqueued: list[tuple[str, float]] = []
    monkeypatch.setattr(chain, "start_chain", _start)
    monkeypatch.setattr(tasks, "enqueue_turn", lambda tid, c: enqueued.append((tid, c)))

    tasks.run_chain_task("run-123")

    assert enqueued == [("turno-1", 0.0)]


def test_run_chain_task_enqueues_nothing_for_a_run_without_selected_turns(
    monkeypatch,
) -> None:
    async def _start(run_id: str) -> None:
        return None

    enqueued: list[tuple[str, float]] = []
    monkeypatch.setattr(chain, "start_chain", _start)
    monkeypatch.setattr(tasks, "enqueue_turn", lambda tid, c: enqueued.append((tid, c)))

    tasks.run_chain_task("run-123")

    assert enqueued == []


def test_score_turn_task_enqueues_the_successor_with_the_pacing_countdown(
    monkeypatch,
) -> None:
    # The pacing between turns is a queue countdown, not a sleep held inside the job.
    async def _advance(turn_id: str) -> chain.NextTurn:
        assert turn_id == "turno-1"
        return chain.NextTurn("turno-2", 4.5)

    enqueued: list[tuple[str, float]] = []
    monkeypatch.setattr(chain, "advance_chain", _advance)
    monkeypatch.setattr(tasks, "enqueue_turn", lambda tid, c: enqueued.append((tid, c)))

    tasks.score_turn_task("turno-1")

    assert enqueued == [("turno-2", 4.5)]


def test_score_turn_task_enqueues_nothing_at_the_end_of_the_chain(monkeypatch) -> None:
    async def _advance(turn_id: str) -> None:
        return None

    enqueued: list[tuple[str, float]] = []
    monkeypatch.setattr(chain, "advance_chain", _advance)
    monkeypatch.setattr(tasks, "enqueue_turn", lambda tid, c: enqueued.append((tid, c)))

    tasks.score_turn_task("turno-9")

    assert enqueued == []


def test_enqueue_run_delegates_to_run_chain_task(monkeypatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(tasks.run_chain_task, "delay", lambda rid: captured.update(id=rid))

    tasks.enqueue_run("run-abc")

    assert captured == {"id": "run-abc"}


def test_enqueue_turn_passes_the_countdown_to_apply_async(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _apply_async(args, countdown):
        captured.update(args=args, countdown=countdown)

    monkeypatch.setattr(tasks.score_turn_task, "apply_async", _apply_async)

    tasks.enqueue_turn("turno-7", 2.5)

    assert captured == {"args": ("turno-7",), "countdown": 2.5}
