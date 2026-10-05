"""Offline tests for ``TypesafeJudge`` — a fake TypeSafe client, no network.

The judge takes an injected ``client`` shaped like ``typesafe_sdk.TypeSafeClient``
(``system_one(state=..., questions=...)``), so these assert on what reaches Jev, how its
``Noul``/``Choice`` answers become ``JudgeDecision``/``JudgeChoice``, and that every call
Jev cannot answer is delegated to the wrapped judge untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scorekeeper.config.settings import Settings
from scorekeeper.core.metrics import judges as judges_pkg
from scorekeeper.core.metrics.base import TurnView
from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics.judges import TracingJudge, TypesafeJudge, make_judge
from scorekeeper.core.metrics.judges import base as judges_base
from scorekeeper.core.metrics.judges.base import (
    CallRecorder,
    JudgeError,
    RetryPolicy,
    UsageAccumulator,
    collect_calls,
    collect_usage,
)
from scorekeeper.core.metrics.scale import Likert

# The judge imports the SDK lazily, but its questions are SDK objects; without the
# ``judges`` extra there is nothing here to test.
typesafe_sdk = pytest.importorskip("typesafe_sdk")
Choice, Noul = typesafe_sdk.Choice, typesafe_sdk.Noul


class FakeTypeSafeClient:
    """Answers every ``system_one`` call with ``answer``, recording the requests.

    ``raises`` is consumed one entry per call (``None`` = succeed), like the provider
    fakes in ``test_judges``.
    """

    def __init__(self, answer: object, raises: list[Exception | None] | None = None) -> None:
        self._answer = answer
        self._raises = list(raises or [])
        self.calls: list[dict] = []

    def system_one(self, *, state, questions):
        self.calls.append({"state": state, "questions": questions})
        failure = self._raises.pop(0) if self._raises else None
        if failure is not None:
            raise failure
        return SimpleNamespace(
            model="jev-1.13.0",
            usage=SimpleNamespace(input_tokens=30, output_tokens=4),
            answers={"decision": self._answer},
        )


def _noul(p: float) -> SimpleNamespace:
    return SimpleNamespace(type="noul", noul=p)


def _judge(answer: object, inner=None, **kwargs) -> tuple[TypesafeJudge, FakeTypeSafeClient]:
    client = FakeTypeSafeClient(answer, **kwargs)
    return TypesafeJudge(inner, client=client), client


def test_decide_asks_a_noul_about_the_turn(turn: TurnView) -> None:
    judge, client = _judge(_noul(0.9))

    decision = judge.decide(instruction="¿Responde a «{prompt}»?", turn=turn)

    assert (decision.value, decision.model) == (True, "jev-1.13.0")
    assert decision.confidence == pytest.approx(0.8)  # |2p - 1|
    (call,) = client.calls
    question = call["questions"]["decision"]
    assert isinstance(question, Noul)
    # The filled template is the question; the turn is the state it is asked about.
    assert question.instructions == f"¿Responde a «{turn.prompt}»?"
    assert call["state"].startswith("--- Turno a evaluar ---")
    assert turn.response in call["state"]


def test_decide_reads_no_and_derives_confidence(turn: TurnView) -> None:
    judge, _ = _judge(_noul(0.4))

    decision = judge.decide(instruction="¿Sí?", turn=turn, step=JudgeStep.VERIFY)

    # p = 0.4 → "no", and |2p - 1| = 0.2: close to a coin toss.
    assert decision.value is False
    assert decision.confidence == pytest.approx(0.2)
    assert decision.justification == ""  # Jev gives none


def test_choose_asks_a_choice_over_the_options(turn: TurnView) -> None:
    judge, client = _judge(
        SimpleNamespace(type="choice", choice="neutral", confidence=0.6, probabilities={})
    )
    options = {"entailment": None, "neutral": None, "contradiction": None}

    choice = judge.choose(instruction="Clasifica", turn=turn, options=options)

    assert (choice.choice, choice.confidence, choice.model) == ("neutral", 0.6, "jev-1.13.0")
    question = client.calls[0]["questions"]["decision"]
    assert isinstance(question, Choice)
    assert dict(question.criteria) == options


def test_non_decision_calls_are_delegated(turn: TurnView, make_judge) -> None:
    inner = make_judge(
        verdicts=[JudgeVerdict(score=4.0, justification="ok", model="m")],
        embeddings={"a": [1.0, 0.0]},
        model="claude-sonnet-5",
    )
    judge, client = _judge(_noul(1.0), inner=inner)

    assert judge.score(rubric="r", turn=turn, scale=Likert()).score == 4.0
    assert judge.embed(texts=["a"]) == [[1.0, 0.0]]
    assert judge.model_for(JudgeStep.EXTRACT) == "claude-sonnet-5"
    assert judge.resolve_model(None, "claude-opus-5") == "claude-opus-5"
    assert [kind for kind, _ in inner.calls] == ["score", "embed"]
    assert client.calls == []


def test_decide_records_usage_and_the_call(turn: TurnView) -> None:
    judge, _ = _judge(_noul(1.0))
    usage, calls = UsageAccumulator(), CallRecorder()

    with collect_usage(usage), collect_calls(calls):
        judge.decide(instruction="¿Sí?", turn=turn, step=JudgeStep.EXTRACT)

    assert (usage.snapshot().input_tokens, usage.snapshot().output_tokens) == (30, 4)
    (record,) = calls.snapshot()
    assert (record.step, record.model) == ("extract", "jev-latest")
    assert (record.input_tokens, record.output_tokens) == (30, 4)
    assert record.prompt.startswith("¿Sí?")


def test_throttled_call_is_retried_by_judge_call(
    turn: TurnView, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TypeSafeAPIError names the HTTP status ``status``, not ``status_code``.
    throttled = RuntimeError("HTTP 429")
    throttled.status = 429  # type: ignore[attr-defined]
    monkeypatch.setattr(judges_base, "retry_policy", lambda: RetryPolicy(3, 0.0, 0.0))
    monkeypatch.setattr(judges_base, "_sleep", lambda seconds: None)
    judge, client = _judge(_noul(1.0), raises=[throttled, None])

    assert judge.decide(instruction="¿Sí?", turn=turn).value is True
    assert len(client.calls) == 2


def test_permanent_failure_is_named(turn: TurnView) -> None:
    judge, _ = _judge(_noul(1.0), raises=[ValueError("bad request")])

    with pytest.raises(JudgeError, match="TypeSafe.*jev-latest.*decisión binaria"):
        judge.decide(instruction="¿Sí?", turn=turn)


def test_tracing_reports_the_model_jev_ran(turn: TurnView, make_judge) -> None:
    traces = []
    inner = make_judge(model="claude-sonnet-5")
    judge, _ = _judge(_noul(1.0), inner=inner)

    TracingJudge(judge, sink=traces.append).decide(instruction="¿Sí?", turn=turn)

    (trace,) = traces
    assert (trace.op, trace.model, trace.ok) == ("decide", "jev-1.13.0", True)


# --- factory wiring -----------------------------------------------------------


def test_make_judge_wraps_decisions_only_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyAnthropic:
        def __init__(self, **kwargs) -> None:
            pass

    built: list[dict] = []

    class DummyTypesafe:
        def __init__(self, inner, **kwargs) -> None:
            self.inner = inner
            built.append(kwargs)

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)
    monkeypatch.setattr(judges_pkg, "TypesafeJudge", DummyTypesafe)

    off = make_judge(Settings(judge_provider="anthropic", anthropic_api_key="sk-a"))
    assert isinstance(off, DummyAnthropic)

    on = make_judge(
        Settings(
            judge_provider="anthropic",
            anthropic_api_key="sk-a",
            typesafe_api_key="ts-key",
            typesafe_judge_model="jev-1.13.0",
            judge_trace_enabled=True,
        )
    )
    # Tracing stays outermost, so decisions answered by Jev are traced too.
    assert isinstance(on, TracingJudge)
    assert isinstance(on._inner, DummyTypesafe)
    assert isinstance(on._inner.inner, DummyAnthropic)
    assert built[0]["api_key"] == "ts-key"
    assert built[0]["model"] == "jev-1.13.0"
