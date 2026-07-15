"""Offline tests for the real judges — fake SDK clients, no network, no LLM SDK.

Each judge takes an injected ``client``, so these run without ``anthropic`` or
``openai`` installed (the SDKs are imported only when a judge builds its own
client). We assert on prompt construction, score clamping, Spanish-justification
passthrough, ``structured()`` return type, and factory selection/errors.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from scorekeeper.config import Settings
from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.judge import JudgeVerdict
from scorekeeper.metrics import judges as judges_pkg
from scorekeeper.metrics.judges import AnthropicJudge, OpenAIJudge, make_judge
from scorekeeper.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    _ScoreResponse,
    clamp,
    render_prompt,
    scale_spec,
)
from scorekeeper.metrics.scale import Boolean, Likert, Unit


class Claims(BaseModel):
    claims: list[str] = []
    summary: str = ""


# --- Fake SDK clients ---------------------------------------------------------


class FakeAnthropicMessages:
    def __init__(self, parsed: object) -> None:
        self._parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return type("Message", (), {"parsed_output": self._parsed})()


class FakeAnthropicClient:
    def __init__(self, parsed: object) -> None:
        self.messages = FakeAnthropicMessages(parsed)


class FakeCompletions:
    def __init__(self, parsed: object) -> None:
        self._parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        message = type("Msg", (), {"parsed": self._parsed})()
        choice = type("Choice", (), {"message": message})()
        return type("Completion", (), {"choices": [choice]})()


class FakeOpenAIClient:
    def __init__(self, parsed: object) -> None:
        self.chat = type("Chat", (), {"completions": FakeCompletions(parsed)})()


# --- base helpers -------------------------------------------------------------


def test_render_prompt_substitutes_placeholders_and_includes_history() -> None:
    turn = TurnView(
        prompt="¿Cómo reinicio el router?",
        response="Mantén pulsado 10s.",
        turn_number=2,
        history=[("Hola", "¡Hola! ¿En qué ayudo?")],
    )
    rendered = render_prompt("Evalúa (1-5): {prompt} {response}", turn)

    assert "¿Cómo reinicio el router?" in rendered
    assert "Mantén pulsado 10s." in rendered
    assert "Historial de la conversación:" in rendered
    assert "Hola" in rendered
    assert "Número de turno: 2" in rendered


def test_render_prompt_includes_retrieved_context() -> None:
    turn = TurnView(
        prompt="¿Cuál es la política de devoluciones?",
        response="30 días.",
        retrieved_context="Devoluciones en 30 días. Requiere recibo.",
    )
    rendered = render_prompt("Evalúa la fidelidad.", turn)

    assert "--- Contexto recuperado ---" in rendered
    assert "Devoluciones en 30 días. Requiere recibo." in rendered


def test_render_prompt_substitutes_context_placeholder() -> None:
    turn = TurnView(
        prompt="p",
        response="r",
        retrieved_context="pasaje A\npasaje B",
    )
    rendered = render_prompt("Contexto: {context}", turn)

    # The {context} placeholder expands to the retrieved-context blob.
    assert "Contexto: pasaje A\npasaje B" in rendered


def test_render_prompt_omits_context_section_when_absent(turn: TurnView) -> None:
    # The default fixture has no retrieved_context, so no section is emitted.
    rendered = render_prompt("Evalúa (1-5): {prompt}", turn)
    assert "--- Contexto recuperado ---" not in rendered


def test_render_prompt_tolerates_stray_braces(turn: TurnView) -> None:
    # A rubric containing an unrelated brace pattern must not raise.
    rendered = render_prompt('Responde en JSON como {"score": 5}. {prompt}', turn)
    assert turn.prompt in rendered


def test_clamp_bounds_and_boolean_rounding() -> None:
    likert = scale_spec(Likert())  # 1..5
    assert clamp(7.0, likert) == 5.0
    assert clamp(0.0, likert) == 1.0
    assert clamp(3.0, likert) == 3.0

    unit = scale_spec(Unit())
    assert clamp(0.42, unit) == 0.42
    assert clamp(1.5, unit) == 1.0

    boolean = scale_spec(Boolean())
    assert clamp(0.7, boolean) == 1.0
    assert clamp(0.3, boolean) == 0.0


# --- AnthropicJudge -----------------------------------------------------------


def test_anthropic_score_returns_verdict_with_model(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=4.0, justification="Correcta y clara."))
    judge = AnthropicJudge(model="claude-test", client=client)

    verdict = judge.score(rubric="Evalúa (1-5): {prompt} {response}", turn=turn, scale=Likert())

    assert isinstance(verdict, JudgeVerdict)
    assert verdict.score == 4.0
    assert verdict.justification == "Correcta y clara."
    assert verdict.model == "claude-test"
    # The rendered rubric + Spanish scale instruction reach the model.
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "Asigna una puntuación entre 1 y 5." in sent


def test_anthropic_score_clamps_out_of_range(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=9.0, justification="Excelente."))
    judge = AnthropicJudge(model="claude-test", client=client)

    verdict = judge.score(rubric="Evalúa", turn=turn, scale=Likert())
    assert verdict.score == 5.0


def test_anthropic_uses_default_system_prompt_and_custom_override(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    AnthropicJudge(model="claude-test", client=client).score(
        rubric="Evalúa", turn=turn, scale=Likert()
    )
    assert client.messages.calls[0]["system"] == DEFAULT_SYSTEM_PROMPT

    custom = "Eres un juez estricto. Responde en español."
    client2 = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    AnthropicJudge(model="claude-test", client=client2, system_prompt=custom).score(
        rubric="Evalúa", turn=turn, scale=Likert()
    )
    assert client2.messages.calls[0]["system"] == custom


def test_openai_custom_system_prompt(turn: TurnView) -> None:
    custom = "Eres un juez estricto. Responde en español."
    client = FakeOpenAIClient(_ScoreResponse(score=1.0, justification="ok"))
    OpenAIJudge(model="gpt-test", client=client, system_prompt=custom).score(
        rubric="verifica", turn=turn, scale=Boolean()
    )
    messages = client.chat.completions.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": custom}


def test_anthropic_structured_returns_schema(turn: TurnView) -> None:
    extraction = Claims(claims=["a", "b"], summary="resumen")
    client = FakeAnthropicClient(extraction)
    judge = AnthropicJudge(model="claude-test", client=client)

    result = judge.structured(instruction="extrae afirmaciones", turn=turn, schema=Claims)
    assert isinstance(result, Claims)
    assert result.claims == ["a", "b"]
    assert client.messages.calls[0]["output_format"] is Claims


# --- OpenAIJudge --------------------------------------------------------------


def test_openai_score_returns_verdict_and_clamps(turn: TurnView) -> None:
    client = FakeOpenAIClient(_ScoreResponse(score=1.0, justification="Cumple el requisito."))
    judge = OpenAIJudge(model="gpt-test", client=client)

    verdict = judge.score(rubric="verifica", turn=turn, scale=Boolean())

    assert verdict.score == 1.0
    assert verdict.justification == "Cumple el requisito."
    assert verdict.model == "gpt-test"


def test_openai_structured_returns_schema(turn: TurnView) -> None:
    client = FakeOpenAIClient(Claims(claims=["x"], summary="s"))
    judge = OpenAIJudge(model="gpt-test", client=client)

    result = judge.structured(instruction="extrae", turn=turn, schema=Claims)
    assert isinstance(result, Claims)
    assert result.claims == ["x"]


# --- Factory ------------------------------------------------------------------


def test_make_judge_selects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict = {}

    class DummyAnthropic:
        def __init__(self, **kwargs) -> None:
            built.update(provider="anthropic", **kwargs)

    class DummyOpenAI:
        def __init__(self, **kwargs) -> None:
            built.update(provider="openai", **kwargs)

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)
    monkeypatch.setattr(judges_pkg, "OpenAIJudge", DummyOpenAI)

    make_judge(
        Settings(
            judge_provider="anthropic",
            anthropic_api_key="sk-a",
            judge_system_prompt="Juez personalizado.",
        )
    )
    assert built["provider"] == "anthropic"
    assert built["api_key"] == "sk-a"
    assert built["system_prompt"] == "Juez personalizado."

    built.clear()
    make_judge(Settings(judge_provider="openai", openai_api_key="sk-o"))
    assert built["provider"] == "openai"
    assert built["api_key"] == "sk-o"
    assert built["system_prompt"] is None


def test_make_judge_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        make_judge(Settings(judge_provider="anthropic", anthropic_api_key=None))
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make_judge(Settings(judge_provider="openai", openai_api_key=None))


def test_make_judge_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="desconocido"):
        make_judge(Settings(judge_provider="gemini"))
