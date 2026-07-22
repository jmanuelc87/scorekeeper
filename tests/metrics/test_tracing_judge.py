"""Tests for the ``TracingJudge`` decorator and its factory wiring.

The decorator is purely observational: it must delegate every call to the inner
judge, pass results/exceptions through untouched, and emit exactly one
``LlmCallTrace`` per call describing what ran. A recording ``sink`` captures the
traces so we can assert on them without a logger.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from scorekeeper.config import Settings
from scorekeeper.metrics import judges as judges_pkg
from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.metrics.judges import TracingJudge, make_judge
from scorekeeper.metrics.judges.tracing import LlmCallTrace
from scorekeeper.metrics.scale import Boolean, Likert


class Claims(BaseModel):
    claims: list[str] = []


class _Recorder:
    """A sink that stores every trace it receives."""

    def __init__(self) -> None:
        self.traces: list[LlmCallTrace] = []

    def __call__(self, trace: LlmCallTrace) -> None:
        self.traces.append(trace)


class _RaisingJudge:
    """Inner judge whose calls all raise, to test error tracing/passthrough."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def model_for(self, step=None) -> str:
        return "modelo-x"

    def score(self, **kwargs) -> JudgeVerdict:
        raise self._exc

    def structured(self, **kwargs):
        raise self._exc

    def embed(self, **kwargs):
        raise self._exc


# --- score --------------------------------------------------------------------


def test_score_delegates_and_traces(turn: TurnView, make_judge) -> None:
    inner = make_judge(
        verdicts=[JudgeVerdict(score=4.0, justification="Correcta.", model="m")],
        model="claude-opus-4-8",
    )
    rec = _Recorder()
    judge = TracingJudge(inner, sink=rec)

    verdict = judge.score(rubric="Evalúa", turn=turn, scale=Likert(), step=JudgeStep.SCORE)

    # Result passes through untouched.
    assert verdict.score == 4.0
    assert verdict.justification == "Correcta."
    # The inner judge actually received the call.
    assert inner.calls == [("score", None)]
    assert inner.steps == [JudgeStep.SCORE]

    # Exactly one trace, describing the call.
    assert len(rec.traces) == 1
    t = rec.traces[0]
    assert t.op == "score"
    assert t.model == "claude-opus-4-8"  # resolved via inner.model_for(step)
    assert t.step == "score"
    assert t.prompt_chars == len(turn.prompt)
    assert t.response_chars == len(turn.response)
    assert t.ok is True
    assert t.error is None
    assert t.latency_ms >= 0.0
    # The verdict score is never recorded in the trace.
    assert not hasattr(t, "score")


def test_explicit_model_wins_over_step_in_trace(turn: TurnView, make_judge) -> None:
    inner = make_judge(
        verdicts=[JudgeVerdict(score=1.0, justification="ok")], model="modelo-por-paso"
    )
    rec = _Recorder()
    judge = TracingJudge(inner, sink=rec)

    judge.score(
        rubric="r", turn=turn, scale=Boolean(), step=JudgeStep.VERIFY, model="modelo-explicito"
    )

    assert rec.traces[0].model == "modelo-explicito"


# --- structured ---------------------------------------------------------------


def test_structured_delegates_and_traces(turn: TurnView, make_judge) -> None:
    inner = make_judge(extractions=[Claims(claims=["a", "b"])], model="modelo-extract")
    rec = _Recorder()
    judge = TracingJudge(inner, sink=rec)

    result = judge.structured(
        instruction="extrae", turn=turn, schema=Claims, step=JudgeStep.EXTRACT
    )

    assert result.claims == ["a", "b"]
    assert inner.calls == [("structured", "Claims")]
    t = rec.traces[0]
    assert t.op == "structured"
    assert t.model == "modelo-extract"
    assert t.step == "extract"


# --- embed --------------------------------------------------------------------


def test_embed_delegates_and_traces(make_judge) -> None:
    inner = make_judge(embeddings={"a": [1.0], "b": [2.0]})
    rec = _Recorder()
    judge = TracingJudge(inner, sink=rec)

    vectors = judge.embed(texts=["a", "b"], model="emb-model")

    assert vectors == [[1.0], [2.0]]
    t = rec.traces[0]
    assert t.op == "embed"
    assert t.step == "embed"
    assert t.model == "emb-model"
    assert t.text_count == 2
    assert t.prompt_chars is None


# --- errors -------------------------------------------------------------------


def test_error_is_traced_and_reraised(turn: TurnView) -> None:
    inner = _RaisingJudge(ValueError("boom"))
    rec = _Recorder()
    judge = TracingJudge(inner, sink=rec)

    with pytest.raises(ValueError, match="boom"):
        judge.score(rubric="r", turn=turn, scale=Boolean())

    # The failure is recorded exactly once, then the exception propagates.
    assert len(rec.traces) == 1
    t = rec.traces[0]
    assert t.ok is False
    assert "boom" in (t.error or "")


def test_model_for_is_forwarded_untraced(make_judge) -> None:
    inner = make_judge(model="modelo-y")
    rec = _Recorder()
    judge = TracingJudge(inner, sink=rec)

    assert judge.model_for(JudgeStep.SCORE) == "modelo-y"
    assert rec.traces == []  # pure resolution, no API call, no trace


# --- default sink -------------------------------------------------------------


def test_default_sink_logs(turn: TurnView, make_judge) -> None:
    from structlog.testing import capture_logs

    inner = make_judge(
        verdicts=[JudgeVerdict(score=2.0, justification="ok")], model="m"
    )
    judge = TracingJudge(inner)  # no sink → default structlog sink

    with capture_logs() as logs:
        judge.score(rubric="r", turn=turn, scale=Boolean(), step=JudgeStep.SCORE)

    # The trace is emitted as a structured event with the fields as key/values.
    assert len(logs) == 1
    event = logs[0]
    assert event["event"] == "llm_call"
    assert event["log_level"] == "info"
    assert event["op"] == "score"
    assert event["model"] == "m"
    assert event["step"] == "score"


# --- factory wiring -----------------------------------------------------------


def test_make_judge_wraps_when_trace_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyAnthropic:
        def __init__(self, **kwargs) -> None:
            pass

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)

    off = make_judge(Settings(judge_provider="anthropic", anthropic_api_key="sk-a"))
    assert isinstance(off, DummyAnthropic)  # default: bare judge, no wrapper

    on = make_judge(
        Settings(
            judge_provider="anthropic", anthropic_api_key="sk-a", judge_trace_enabled=True
        )
    )
    assert isinstance(on, TracingJudge)
