"""Live Claude model list: refresh, resilience, ownership and scheduling (no network)."""

from __future__ import annotations

import httpx2
import pytest

from scorekeeper.celery_app import _on_worker_process_init
from scorekeeper.config.settings import Settings
from scorekeeper.core.metrics.judges import claude_models
from scorekeeper.core.metrics.judges.agent_judge import AgentJudge
from scorekeeper.core.metrics.judges.anthropic_judge import (
    KNOWN_MODELS,
    AnthropicJudge,
)


@pytest.fixture(autouse=True)
def _reset_listed(monkeypatch):
    monkeypatch.setattr(claude_models, "_listed", None)


def _client(pages: dict[str | None, dict]) -> httpx2.Client:
    """An httpx client serving ``pages`` keyed by ``after_id``; asserts the auth headers."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["x-api-key"] == "k"
        return httpx2.Response(200, json=pages[request.url.params.get("after_id")])

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def _page(ids: list[str], *, has_more: bool, last: str | None = None) -> dict:
    return {
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"}
            for i in ids
        ],
        "has_more": has_more,
        "first_id": ids[0],
        "last_id": last or ids[-1],
    }


def test_refresh_follows_pagination() -> None:
    client = _client(
        {
            None: _page(["claude-sonnet-5-5", "claude-opus-5"], has_more=True),
            "claude-opus-5": _page(["claude-haiku-5"], has_more=False),
        }
    )

    assert claude_models.refresh_models("k", client=client) is True
    assert claude_models.listed_models() == {
        "claude-sonnet-5-5",
        "claude-opus-5",
        "claude-haiku-5",
    }


def test_failure_keeps_previous_list(monkeypatch) -> None:
    monkeypatch.setattr(claude_models, "_listed", frozenset({"claude-opus-5"}))

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("red caida")

    client = httpx2.Client(transport=httpx2.MockTransport(boom))

    assert claude_models.refresh_models("k", client=client) is False
    assert claude_models.listed_models() == {"claude-opus-5"}


def test_no_key_falls_back_to_seed() -> None:
    assert claude_models.refresh_models(None) is False
    assert claude_models.listed_models() is None
    judge = AnthropicJudge(model="claude-opus-4-8", client=object())
    for model in KNOWN_MODELS:
        assert judge.resolve_model(None, model) == model


@pytest.mark.parametrize("judge_cls", [AnthropicJudge, AgentJudge])
def test_listed_model_accepted_and_unknown_rejected(judge_cls) -> None:
    judge = judge_cls(model="claude-opus-4-8", client=object())
    with pytest.raises(ValueError):
        judge.resolve_model(None, "claude-sonnet-5-5")

    claude_models.refresh_models(
        "k", client=_client({None: _page(["claude-sonnet-5-5"], has_more=False)})
    )

    assert judge.resolve_model(None, "claude-sonnet-5-5") == "claude-sonnet-5-5"
    with pytest.raises(ValueError):
        judge.resolve_model(None, "claude-opus-6")


def test_listed_model_outside_capability_sets_omits_thinking() -> None:
    assert AnthropicJudge._thinking_kwargs("claude-sonnet-5-5") == {}


def test_default_interval_is_four_weeks() -> None:
    assert Settings().claude_models_refresh_interval_seconds == 4 * 7 * 24 * 3600


def test_periodic_refresh_runs_now_then_waits_the_interval(monkeypatch) -> None:
    calls: list[str | None] = []
    waits: list[float] = []

    class Stop:
        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            return len(waits) >= 2  # allow one repeat, then stop

    class ImmediateThread:
        def __init__(self, target, **_kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(claude_models, "refresh_models", lambda key: calls.append(key))
    monkeypatch.setattr(claude_models.threading, "Event", Stop)
    monkeypatch.setattr(claude_models.threading, "Thread", ImmediateThread)

    claude_models.start_periodic_refresh("key", 4 * 7 * 24 * 3600.0)

    assert calls == ["key", "key"]
    assert waits == [2419200.0, 2419200.0]


def test_worker_process_init_starts_refresh_with_settings(monkeypatch) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(
        "scorekeeper.celery_app.start_periodic_refresh", lambda *a: seen.append(a)
    )
    _on_worker_process_init()
    assert seen and seen[0][1] == 4 * 7 * 24 * 3600.0
