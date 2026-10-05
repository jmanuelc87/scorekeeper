"""Offline tests for the real judges — fake SDK clients, no network, no LLM SDK.

Each judge takes an injected ``client``, so these run without ``anthropic`` or
``openai`` installed (the SDKs are imported only when a judge builds its own
client). We assert on prompt construction, score clamping, Spanish-justification
passthrough, ``structured()`` return type, and factory selection/errors.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from scorekeeper.config.settings import Settings
from scorekeeper.core.metrics.base import TurnView
from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics import judges as judges_pkg
from scorekeeper.core.metrics.judges import (
    AgentJudge,
    AnthropicJudge,
    OpenAIJudge,
    make_judge,
)
from scorekeeper.core.metrics.judges import base as judges_base
from scorekeeper.core.metrics.judges.base import (
    DECISION_INSTRUCTION_ES,
    DEFAULT_SYSTEM_PROMPT,
    CallRecorder,
    JudgeError,
    RetryPolicy,
    StepModels,
    UsageAccumulator,
    _DecisionResponse,
    _ScoreResponse,
    clamp,
    collect_calls,
    collect_usage,
    render_prompt,
    scale_spec,
)
from scorekeeper.core.metrics.scale import Boolean, Likert, Unit
from scorekeeper.core.retrieved_context import Chunk, RetrievedContext, RetrievedDocument


class Claims(BaseModel):
    claims: list[str] = []
    summary: str = ""


# --- Fake SDK clients ---------------------------------------------------------


class _FailureScript:
    """What a fake client raises on successive calls.

    A single exception raises on *every* call. A list is consumed one entry per call,
    where ``None`` means "this call succeeds" — which is how a test expresses "throttled
    twice, then fine" and so can observe ``judge_call`` retrying.
    """

    def __init__(self, raises: Exception | list[Exception | None] | None) -> None:
        self._raises = raises

    def check(self) -> None:
        if isinstance(self._raises, list):
            failure = self._raises.pop(0) if self._raises else None
        else:
            failure = self._raises
        if failure is not None:
            raise failure


def throttled(status: int = 429, retry_after: str | None = None) -> Exception:
    """An SDK-shaped capacity error: a ``status_code``, plus ``Retry-After`` when given.

    Hand-rolled rather than imported from a provider SDK, matching the rest of these
    fakes — ``judge_call`` classifies by duck-typed ``status_code``, not by SDK class.
    """
    exc = RuntimeError(f"HTTP {status}")
    exc.status_code = status  # type: ignore[attr-defined]
    if retry_after is not None:
        exc.response = SimpleNamespace(headers={"retry-after": retry_after})  # type: ignore[attr-defined]
    return exc


class FakeAnthropicMessages:
    def __init__(
        self,
        parsed: object,
        raises: Exception | list[Exception | None] | None = None,
        usage: object | None = None,
    ) -> None:
        self._parsed = parsed
        self._failures = _FailureScript(raises)
        self._usage = usage
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        self._failures.check()
        attrs: dict[str, object] = {"parsed_output": self._parsed}
        # Only attach `usage` when configured, so tests that omit it exercise the
        # getattr-safe path (no usage recorded).
        if self._usage is not None:
            attrs["usage"] = self._usage
        return type("Message", (), attrs)()


class FakeAnthropicClient:
    """Fake with both namespaces, each recording its own calls.

    ``fallbacks`` lives on ``client.beta.messages``, so the two logs are what lets a
    test assert *which* namespace a call took — the judge must only reach for the beta
    one when it actually sends the parameter.
    """

    def __init__(
        self,
        parsed: object,
        raises: Exception | list[Exception | None] | None = None,
        usage: object | None = None,
    ) -> None:
        self.messages = FakeAnthropicMessages(parsed, raises, usage)
        self.beta = SimpleNamespace(
            messages=FakeAnthropicMessages(parsed, raises, usage)
        )


class FakeCompletions:
    def __init__(
        self,
        parsed: object,
        refusal: str | None = None,
        usage: object | None = None,
        raises: Exception | list[Exception | None] | None = None,
    ) -> None:
        self._parsed = parsed
        self._refusal = refusal
        self._usage = usage
        self._failures = _FailureScript(raises)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        self._failures.check()
        message = type("Msg", (), {"parsed": self._parsed, "refusal": self._refusal})()
        choice = type("Choice", (), {"message": message})()
        attrs: dict[str, object] = {"choices": [choice]}
        if self._usage is not None:
            attrs["usage"] = self._usage
        return type("Completion", (), attrs)()


class FakeEmbeddings:
    def __init__(
        self,
        vectors: list[list[float]],
        usage: object | None = None,
        raises: Exception | list[Exception | None] | None = None,
    ) -> None:
        self._vectors = vectors
        self._usage = usage
        self._failures = _FailureScript(raises)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        self._failures.check()
        data = [type("Emb", (), {"embedding": v})() for v in self._vectors]
        attrs: dict[str, object] = {"data": data}
        if self._usage is not None:
            attrs["usage"] = self._usage
        return type("Response", (), attrs)()


class FakeAgentSdk:
    """Stands in for the ``claude_agent_sdk`` module the agent judge drives.

    The judge uses exactly two names from it — ``ClaudeAgentOptions`` (a plain option
    bag) and ``query`` (an async iterator of messages ending in a result message) — so
    these tests run without the SDK installed. ``structured_output`` is what the judge
    duck-types the result message on; the leading text message stands in for the
    assistant chatter the CLI streams before it.
    """

    def __init__(
        self,
        structured_output: object,
        *,
        usage: dict | None = None,
        is_error: bool = False,
        api_error_status: int | None = None,
        errors: list[str] | None = None,
        raises: Exception | list[Exception | None] | None = None,
    ) -> None:
        self._structured_output = structured_output
        self._usage = usage
        self._is_error = is_error
        self._api_error_status = api_error_status
        self._errors = errors
        self._failures = _FailureScript(raises)
        self.calls: list[dict] = []

    def ClaudeAgentOptions(self, **kwargs):  # noqa: N802 — mirrors the SDK's class name
        return SimpleNamespace(**kwargs)

    def query(self, **kwargs):
        self.calls.append(kwargs)
        self._failures.check()
        return self._messages()

    async def _messages(self):
        yield SimpleNamespace(content="pensando…")  # not the result message
        yield SimpleNamespace(
            structured_output=self._structured_output,
            is_error=self._is_error,
            api_error_status=self._api_error_status,
            errors=self._errors,
            subtype="error_during_execution" if self._is_error else "success",
            usage=self._usage,
        )


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    def __init__(
        self,
        parsed: object,
        embeddings: list[list[float]] | None = None,
        refusal: str | None = None,
        usage: object | None = None,
        embed_usage: object | None = None,
        raises: Exception | list[Exception | None] | None = None,
        embed_raises: Exception | list[Exception | None] | None = None,
    ) -> None:
        self.chat = FakeChat(FakeCompletions(parsed, refusal, usage, raises))
        self.embeddings = FakeEmbeddings(embeddings or [], embed_usage, embed_raises)


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
        retrieved_context=RetrievedContext(
            documents=[
                RetrievedDocument(
                    name="Política de devoluciones",
                    document="manual.pdf",
                    url="https://ejemplo.com/manual",
                    chunks=[Chunk(index=0, text="Devoluciones en 30 días. Requiere recibo.")],
                )
            ]
        ),
    )
    rendered = render_prompt("Evalúa la fidelidad. {context}", turn)

    # The context is delimited with <contexto> tags so the judge can tell it apart.
    assert "<contexto>\n" in rendered
    assert "\n</contexto>" in rendered
    # The rendered context surfaces the content plus its label and source metadata.
    assert "Devoluciones en 30 días. Requiere recibo." in rendered
    assert "Política de devoluciones" in rendered
    assert "manual.pdf" in rendered
    assert "https://ejemplo.com/manual" in rendered


def test_render_prompt_substitutes_context_placeholder() -> None:
    turn = TurnView(
        prompt="p",
        response="r",
        retrieved_context=RetrievedContext(
            documents=[
                RetrievedDocument(
                    name="",
                    document="",
                    chunks=[Chunk(index=0, text="pasaje A\npasaje B")],
                )
            ]
        ),
    )
    rendered = render_prompt("Contexto: {context}", turn)

    # The {context} placeholder expands to the retrieved context inside <contexto> tags.
    assert "Contexto: <contexto>\npasaje A\npasaje B\n</contexto>" in rendered


def test_render_prompt_renders_web_citations_as_readable_docs() -> None:
    # Web citations captured from a chat UI (a name and a url, no fetched content) reach
    # the judge as readable label/url blocks — the retrieval pipeline has already parsed
    # them out of any raw JSON, so the judge never sees the JSON itself.
    turn = TurnView(
        prompt="¿Cuál es la política de devoluciones?",
        response="30 días.",
        retrieved_context=RetrievedContext(
            documents=[
                RetrievedDocument(
                    name="Política", document="", url="https://a/pol"
                ),
                RetrievedDocument(
                    name="", document="", url="https://b/x"
                ),
            ]
        ),
    )
    rendered = render_prompt("Contexto: {context}", turn)

    assert "Política" in rendered
    assert "https://a/pol" in rendered
    assert "https://b/x" in rendered
    assert '{"name"' not in rendered  # raw JSON never reaches the judge.


def test_render_prompt_omits_context_when_the_template_does_not_ask_for_it() -> None:
    # The context reaches the judge only through {context}: a template without it —
    # hallucination.nli, answer_relevance.generate_question — never sees the blob.
    turn = TurnView(
        prompt="¿Cuál es la política de devoluciones?",
        response="30 días.",
        retrieved_context=RetrievedContext(
            documents=[
                RetrievedDocument(
                    name="",
                    document="",
                    chunks=[Chunk(index=0, text="Devoluciones en 30 días.")],
                )
            ]
        ),
    )
    rendered = render_prompt("Evalúa (1-5): {prompt}", turn)

    assert "Devoluciones en 30 días." not in rendered
    assert "<contexto>" not in rendered


def test_render_prompt_leaves_context_placeholder_empty_without_context(
    turn: TurnView,
) -> None:
    # No context → the placeholder expands to nothing, not to empty <contexto> tags.
    rendered = render_prompt("Contexto: {context}", turn)
    assert "<contexto>" not in rendered


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
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    verdict = judge.score(rubric="Evalúa (1-5): {prompt} {response}", turn=turn, scale=Likert())

    assert isinstance(verdict, JudgeVerdict)
    assert verdict.score == 4.0
    assert verdict.justification == "Correcta y clara."
    assert verdict.model == "claude-opus-4-8"
    # The rendered rubric + Spanish scale instruction reach the model.
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "Asigna una puntuación entre 1 y 5." in sent


def test_anthropic_score_clamps_out_of_range(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=9.0, justification="Excelente."))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    verdict = judge.score(rubric="Evalúa", turn=turn, scale=Likert())
    assert verdict.score == 5.0


def test_anthropic_uses_default_system_prompt_and_custom_override(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    AnthropicJudge(model="claude-opus-4-8", client=client).score(
        rubric="Evalúa", turn=turn, scale=Likert()
    )
    assert client.messages.calls[0]["system"] == DEFAULT_SYSTEM_PROMPT

    custom = "Eres un juez estricto. Responde en español."
    client2 = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    AnthropicJudge(model="claude-opus-4-8", client=client2, system_prompt=custom).score(
        rubric="Evalúa", turn=turn, scale=Likert()
    )
    assert client2.messages.calls[0]["system"] == custom


def test_openai_custom_system_prompt(turn: TurnView) -> None:
    custom = "Eres un juez estricto. Responde en español."
    client = FakeOpenAIClient(_ScoreResponse(score=1.0, justification="ok"))
    OpenAIJudge(model="gpt-5.6-sol", client=client, system_prompt=custom).score(
        rubric="verifica", turn=turn, scale=Boolean()
    )
    messages = client.chat.completions.calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": custom}


def test_anthropic_structured_returns_schema(turn: TurnView) -> None:
    extraction = Claims(claims=["a", "b"], summary="resumen")
    client = FakeAnthropicClient(extraction)
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    result = judge.structured(instruction="extrae afirmaciones", turn=turn, schema=Claims)
    assert isinstance(result, Claims)
    assert result.claims == ["a", "b"]
    assert client.messages.calls[0]["output_format"] is Claims


# --- Decisions on an LLM judge ------------------------------------------------


def test_llm_decide_is_a_structured_call_on_the_routed_model(turn: TurnView) -> None:
    client = FakeAnthropicClient(
        _DecisionResponse(answer=True, confidence=1.4, justification="Se deduce.")
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    decision = judge.decide(instruction="¿Se deduce?", turn=turn, model="claude-sonnet-5")

    assert decision.value is True
    assert decision.confidence == 1.0  # clamped into [0, 1]
    assert decision.justification == "Se deduce."
    assert decision.model == "claude-sonnet-5"
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["output_format"] is _DecisionResponse
    # The template's question, then the instruction fixing the boolean's polarity.
    content = call["messages"][0]["content"]
    assert content.startswith(f"¿Se deduce?\n\n{DECISION_INSTRUCTION_ES}")


def test_llm_choose_constrains_the_answer_to_the_options(turn: TurnView) -> None:
    client = FakeAnthropicClient(
        SimpleNamespace(choice="neutral", confidence=0.7, justification="Ni sí ni no.")
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    choice = judge.choose(
        instruction="Clasifica", turn=turn, options={"entailment": None, "neutral": None}
    )

    assert (choice.choice, choice.confidence, choice.model) == (
        "neutral",
        0.7,
        "claude-opus-4-8",
    )
    schema = client.messages.calls[0]["output_format"]
    schema.model_validate({"choice": "entailment", "confidence": 1, "justification": ""})
    with pytest.raises(ValueError):
        schema.model_validate({"choice": "otra", "confidence": 1, "justification": ""})


def test_capacity_error_is_read_from_a_status_attribute() -> None:
    exc = RuntimeError("HTTP 429")
    exc.status = 429  # type: ignore[attr-defined]  # typesafe_sdk's spelling

    assert judges_base._is_capacity_error(exc)


# --- Per-step model routing ---------------------------------------------------


def test_anthropic_routes_model_per_step(turn: TurnView) -> None:
    # EXTRACT and VERIFY get their own models; the unmapped SCORE step and a None
    # step fall back to the judge's default model, and the returned verdict/model
    # reflects the model actually used.
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(
        model="claude-opus-4-8",
        client=client,
        step_models=StepModels(
            "claude-opus-4-8",
            {
                JudgeStep.EXTRACT: "claude-haiku-4-5-20251001",
                JudgeStep.VERIFY: "claude-sonnet-5",
            },
        ),
    )

    verify = judge.score(rubric="v", turn=turn, scale=Boolean(), step=JudgeStep.VERIFY)
    assert verify.model == "claude-sonnet-5"
    assert client.messages.calls[-1]["model"] == "claude-sonnet-5"

    judge.structured(instruction="x", turn=turn, schema=Claims, step=JudgeStep.EXTRACT)
    assert client.messages.calls[-1]["model"] == "claude-haiku-4-5-20251001"

    # SCORE is unmapped, and step=None both resolve to the default model.
    scored = judge.score(rubric="s", turn=turn, scale=Unit(), step=JudgeStep.SCORE)
    assert scored.model == "claude-opus-4-8"
    default = judge.score(rubric="s", turn=turn, scale=Unit())
    assert default.model == "claude-opus-4-8"


def test_anthropic_thinking_is_per_model(turn: TurnView) -> None:
    # Adaptive thinking is a 4.6+ feature: Opus/Sonnet calls send it, but Haiku 4.5
    # does not support it and would 400, so those calls omit ``thinking`` entirely.
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-opus-4-8")
    assert client.messages.calls[-1]["thinking"] == {"type": "adaptive"}

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-sonnet-5")
    assert client.messages.calls[-1]["thinking"] == {"type": "adaptive"}

    # Opus 5 thinks by default and Fable 5 always thinks: for both, ``adaptive`` is
    # the only configuration the API accepts, so it must never be omitted.
    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-opus-5")
    assert client.messages.calls[-1]["thinking"] == {"type": "adaptive"}

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-fable-5")
    assert client.messages.calls[-1]["thinking"] == {"type": "adaptive"}

    # Haiku (bulk tier) via both seam methods → no thinking kwarg at all.
    judge.score(rubric="s", turn=turn, scale=Boolean(), model="claude-haiku-4-5-20251001")
    assert "thinking" not in client.messages.calls[-1]

    judge.structured(
        instruction="x", turn=turn, schema=Claims, model="claude-haiku-4-5-20251001"
    )
    assert "thinking" not in client.messages.calls[-1]


def test_openai_routes_model_per_step(turn: TurnView) -> None:
    client = FakeOpenAIClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = OpenAIJudge(
        model="gpt-5.6-sol",
        client=client,
        step_models=StepModels("gpt-5.6-sol", {JudgeStep.VERIFY: "gpt-4o-mini"}),
    )

    verify = judge.score(rubric="v", turn=turn, scale=Boolean(), step=JudgeStep.VERIFY)
    assert verify.model == "gpt-4o-mini"
    assert client.chat.completions.calls[-1]["model"] == "gpt-4o-mini"

    default = judge.score(rubric="s", turn=turn, scale=Unit(), step=JudgeStep.SCORE)
    assert default.model == "gpt-5.6-sol"


def test_judge_without_step_models_uses_single_model(turn: TurnView) -> None:
    # No step_models → every step resolves to ``model`` (back-compat).
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    for step in (None, JudgeStep.EXTRACT, JudgeStep.VERIFY, JudgeStep.SCORE):
        verdict = judge.score(rubric="r", turn=turn, scale=Boolean(), step=step)
        assert verdict.model == "claude-opus-4-8"


def test_step_models_resolution() -> None:
    models = StepModels("base", {JudgeStep.EXTRACT: "cheap", JudgeStep.VERIFY: ""})
    assert models.for_step(JudgeStep.EXTRACT) == "cheap"
    assert models.for_step(JudgeStep.VERIFY) == "base"  # falsy override ignored
    assert models.for_step(JudgeStep.SCORE) == "base"  # unmapped
    assert models.for_step(None) == "base"
    assert models.for_step("extract") == "cheap"  # accepts the raw value too


# --- OpenAIJudge --------------------------------------------------------------


def test_openai_score_returns_verdict_and_clamps(turn: TurnView) -> None:
    client = FakeOpenAIClient(_ScoreResponse(score=1.0, justification="Cumple el requisito."))
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    verdict = judge.score(rubric="verifica", turn=turn, scale=Boolean())

    assert verdict.score == 1.0
    assert verdict.justification == "Cumple el requisito."
    assert verdict.model == "gpt-5.6-sol"


def test_openai_structured_returns_schema(turn: TurnView) -> None:
    client = FakeOpenAIClient(Claims(claims=["x"], summary="s"))
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    result = judge.structured(instruction="extrae", turn=turn, schema=Claims)
    assert isinstance(result, Claims)
    assert result.claims == ["x"]


def test_openai_embed_returns_vectors_in_order() -> None:
    client = FakeOpenAIClient(None, embeddings=[[1.0, 0.0], [0.0, 1.0]])
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client, embedding_model="emb-test")

    vectors = judge.embed(texts=["a", "b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    call = client.embeddings.calls[0]
    assert call["model"] == "emb-test"
    assert call["input"] == ["a", "b"]


def test_openai_judge_forwards_base_url_to_the_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazily-built client targets ``base_url`` (None → the SDK default)."""
    built: list[dict] = []

    def fake_openai(**kwargs) -> object:
        built.append(kwargs)
        return object()

    monkeypatch.setitem(
        sys.modules, "openai", SimpleNamespace(OpenAI=fake_openai)
    )

    OpenAIJudge(model="gpt-5.6-sol", api_key="sk-o")
    assert built[-1]["base_url"] is None

    OpenAIJudge(model="gpt-5.6-sol", api_key="sk-o", base_url="http://localhost:1234/v1")
    assert built[-1]["base_url"] == "http://localhost:1234/v1"


def test_openai_embed_empty_skips_call() -> None:
    client = FakeOpenAIClient(None, embeddings=[[1.0]])
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    assert judge.embed(texts=[]) == []
    assert client.embeddings.calls == []


def test_anthropic_embed_delegates_to_embedder(turn: TurnView) -> None:
    embed_client = FakeOpenAIClient(None, embeddings=[[0.5, 0.5]])
    embedder = OpenAIJudge(model="gpt-5.6-sol", client=embed_client)
    client = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client, embedder=embedder)

    assert judge.embed(texts=["a"]) == [[0.5, 0.5]]


def test_anthropic_embed_without_backend_raises(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(NotImplementedError, match="embeddings"):
        judge.embed(texts=["a"])


# --- Explicit model selection + provider validation ---------------------------


def test_explicit_model_overrides_step_routing(turn: TurnView) -> None:
    # An explicit ``model=`` is used verbatim and wins over the step router.
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(
        model="claude-opus-4-8",
        client=client,
        step_models=StepModels(
            "claude-opus-4-8", {JudgeStep.VERIFY: "claude-sonnet-5"}
        ),
    )

    verdict = judge.score(
        rubric="v",
        turn=turn,
        scale=Boolean(),
        step=JudgeStep.VERIFY,
        model="claude-haiku-4-5-20251001",
    )
    assert verdict.model == "claude-haiku-4-5-20251001"
    assert client.messages.calls[-1]["model"] == "claude-haiku-4-5-20251001"


def test_anthropic_rejects_foreign_model(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(ValueError, match="no pertenece"):
        judge.score(rubric="r", turn=turn, scale=Boolean(), model="gpt-5.6-sol")


def test_openai_rejects_foreign_model(turn: TurnView) -> None:
    client = FakeOpenAIClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    with pytest.raises(ValueError, match="no pertenece"):
        judge.structured(
            instruction="x", turn=turn, schema=Claims, model="claude-opus-4-8"
        )


def test_model_for_resolves_and_validates(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(
        model="claude-opus-4-8",
        client=client,
        step_models=StepModels(
            "claude-opus-4-8", {JudgeStep.EXTRACT: "claude-haiku-4-5-20251001"}
        ),
    )

    assert judge.model_for(JudgeStep.EXTRACT) == "claude-haiku-4-5-20251001"
    assert judge.model_for(JudgeStep.SCORE) == "claude-opus-4-8"
    assert judge.model_for(None) == "claude-opus-4-8"

    # A step override pointing at another provider's model surfaces as a per-call
    # ValueError (the global-settings leakage becomes visible at point of use).
    leaked = AnthropicJudge(
        model="claude-opus-4-8",
        client=client,
        step_models=StepModels(
            "claude-opus-4-8", {JudgeStep.VERIFY: "gpt-5.6-sol"}
        ),
    )
    with pytest.raises(ValueError, match="no pertenece"):
        leaked.model_for(JudgeStep.VERIFY)


def test_openai_embed_model_override_validates(turn: TurnView) -> None:
    client = FakeOpenAIClient(None, embeddings=[[1.0]])
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    # An explicit embedding model overrides the default and is passed to the SDK.
    judge.embed(texts=["a"], model="text-embedding-3-large")
    assert client.embeddings.calls[-1]["model"] == "text-embedding-3-large"

    # A chat model is not a valid embedding model → Spanish ValueError.
    with pytest.raises(ValueError, match="no pertenece"):
        judge.embed(texts=["a"], model="gpt-5.6-sol")


# --- Claude Agent judge -------------------------------------------------------


def test_agent_score_returns_verdict_and_clamps(turn: TurnView) -> None:
    # The CLI returns structured output as a plain dict; the judge validates it against
    # the schema and clamps the score into the scale.
    client = FakeAgentSdk({"score": 9.0, "justification": "Muy claro"})
    judge = AgentJudge(model="claude-opus-4-8", client=client)

    verdict = judge.score(rubric="Evalúa la claridad", turn=turn, scale=Likert())

    assert verdict == JudgeVerdict(
        score=5.0, justification="Muy claro", model="claude-opus-4-8"
    )


def test_agent_query_is_tool_free_with_a_json_schema(turn: TurnView) -> None:
    # The agent judge evaluates a rubric, it does not act: no tools, and the answer is
    # pinned to the score schema. Adaptive thinking follows the model.
    client = FakeAgentSdk({"score": 1.0, "justification": "ok"})
    judge = AgentJudge(model="claude-opus-4-8", client=client, system_prompt="Juez.")

    judge.score(rubric="Evalúa", turn=turn, scale=Unit())

    options = client.calls[0]["options"]
    assert options.tools == []
    assert options.max_turns == 5
    assert options.model == "claude-opus-4-8"
    assert options.system_prompt == "Juez."
    assert options.thinking == {"type": "adaptive"}
    assert options.output_format == {
        "type": "json_schema",
        "schema": _ScoreResponse.model_json_schema(),
    }
    assert "Evalúa" in client.calls[0]["prompt"]


def test_agent_forwards_the_oauth_token_to_the_cli(turn: TurnView) -> None:
    # In Docker there is no Claude Code session to borrow, so the token is what
    # authenticates the CLI subprocess the judge spawns.
    client = FakeAgentSdk({"score": 1.0, "justification": "ok"})
    judge = AgentJudge(model="claude-opus-4-8", client=client, oauth_token="sk-ant-oat-x")

    judge.score(rubric="Evalúa", turn=turn, scale=Unit())

    assert client.calls[0]["options"].env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-x"}


def test_agent_without_a_token_leaves_the_cli_environment_alone(turn: TurnView) -> None:
    # No token configured → nothing is injected, so a local run keeps using whatever
    # credentials the inherited environment already carries.
    client = FakeAgentSdk({"score": 1.0, "justification": "ok"})
    judge = AgentJudge(model="claude-opus-4-8", client=client)

    judge.score(rubric="Evalúa", turn=turn, scale=Unit())

    assert client.calls[0]["options"].env == {}


def test_agent_omits_thinking_for_models_without_adaptive_support(turn: TurnView) -> None:
    # Haiku rejects adaptive thinking, so the option is left off entirely.
    client = FakeAgentSdk({"score": 1.0, "justification": "ok"})
    judge = AgentJudge(model="claude-haiku-4-5-20251001", client=client)

    judge.score(rubric="Evalúa", turn=turn, scale=Boolean())

    assert not hasattr(client.calls[0]["options"], "thinking")


def test_agent_structured_returns_schema(turn: TurnView) -> None:
    client = FakeAgentSdk({"claims": ["a", "b"], "summary": "resumen"})
    judge = AgentJudge(model="claude-opus-4-8", client=client)

    result = judge.structured(instruction="Extrae", turn=turn, schema=Claims)

    assert isinstance(result, Claims)
    assert result.claims == ["a", "b"]
    assert client.calls[0]["options"].output_format["schema"] == Claims.model_json_schema()


def test_agent_records_usage_from_the_result_message(turn: TurnView) -> None:
    # The SDK reports usage as a plain dict, with the prompt-cached input split out of
    # input_tokens: the cache counters must be added back or the call reads as ~free.
    client = FakeAgentSdk(
        {"score": 3.0, "justification": "ok"},
        usage={
            "input_tokens": 2,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 2,
            "output_tokens": 6,
        },
    )
    judge = AgentJudge(model="claude-opus-4-8", client=client)
    acc = UsageAccumulator()
    with collect_usage(acc):
        judge.score(rubric="r", turn=turn, scale=Likert())

    snap = acc.snapshot()
    assert (snap.input_tokens, snap.output_tokens) == (14, 6)


def test_agent_rejects_foreign_model(turn: TurnView) -> None:
    judge = AgentJudge(model="claude-opus-4-8", client=FakeAgentSdk(None))
    with pytest.raises(ValueError, match="no pertenece al proveedor"):
        judge.score(rubric="r", turn=turn, scale=Likert(), model="gpt-4o")


def test_agent_missing_structured_output_raises_descriptive_error(turn: TurnView) -> None:
    # The query succeeded but produced nothing matching the schema.
    judge = AgentJudge(model="claude-opus-4-8", client=FakeAgentSdk(None))

    with pytest.raises(JudgeError) as exc_info:
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())
    message = str(exc_info.value)
    assert "Claude Agent" in message
    assert "claude-opus-4-8" in message
    assert "puntuación" in message


def test_agent_throttled_result_is_retried_then_succeeds(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    # The CLI reports a throttled call as an error *result message*, not by raising, so
    # the judge has to turn it back into a retryable failure for judge_call.
    set_policy(max_attempts=3)
    client = FakeAgentSdk(
        None, is_error=True, api_error_status=429, errors=["rate limit"]
    )
    judge = AgentJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError, match="rate limit"):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert len(client.calls) == 3  # retried until the budget ran out
    assert backoff_waits == [1.0, 2.0]


def test_agent_error_without_a_status_is_not_retried(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    # A CLI failure with no API status is permanent: reporting it once beats sleeping.
    set_policy(max_attempts=3)
    client = FakeAgentSdk(None, is_error=True, errors=["sesión no autenticada"])
    judge = AgentJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError, match="sesión no autenticada"):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert len(client.calls) == 1
    assert backoff_waits == []


def test_agent_embed_delegates_to_embedder_and_raises_without_one() -> None:
    embedder = OpenAIJudge(
        model="gpt-5.6-sol", client=FakeOpenAIClient(None, embeddings=[[1.0, 0.0]])
    )
    judge = AgentJudge(model="claude-opus-4-8", client=FakeAgentSdk(None), embedder=embedder)
    assert judge.embed(texts=["a"]) == [[1.0, 0.0]]

    bare = AgentJudge(model="claude-opus-4-8", client=FakeAgentSdk(None))
    with pytest.raises(NotImplementedError, match="embeddings"):
        bare.embed(texts=["a"])


# --- Descriptive errors -------------------------------------------------------


def test_anthropic_score_missing_output_raises_descriptive_error(turn: TurnView) -> None:
    # The model returned nothing that satisfies the schema (parsed_output is None):
    # instead of an opaque AttributeError, the judge names the provider, model, and step.
    client = FakeAnthropicClient(None)
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError) as exc_info:
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())
    message = str(exc_info.value)
    assert "Anthropic" in message
    assert "claude-opus-4-8" in message
    assert "puntuación" in message


def test_openai_refusal_surfaces_refusal_text(turn: TurnView) -> None:
    # An OpenAI refusal (parsed is None, refusal set) reaches the caller verbatim.
    client = FakeOpenAIClient(None, refusal="No puedo ayudar con eso.")
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    with pytest.raises(JudgeError, match="No puedo ayudar con eso."):
        judge.score(rubric="verifica", turn=turn, scale=Boolean())


def test_anthropic_sdk_error_is_wrapped_with_call_context(turn: TurnView) -> None:
    # A raw SDK/transport error is wrapped so the message names the failing call and
    # chains the original exception rather than propagating it bare.
    client = FakeAnthropicClient(None, raises=RuntimeError("429 rate limit"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError) as exc_info:
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())
    message = str(exc_info.value)
    assert "claude-opus-4-8" in message
    assert "429 rate limit" in message
    assert isinstance(exc_info.value.__cause__, RuntimeError)


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


def test_make_judge_anthropic_wires_embedder_when_openai_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict = {}

    class DummyAnthropic:
        def __init__(self, **kwargs) -> None:
            built.update(provider="anthropic", **kwargs)

    class DummyOpenAI:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs  # stands in for the embedder backend

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)
    monkeypatch.setattr(judges_pkg, "OpenAIJudge", DummyOpenAI)

    # No OpenAI key: no embeddings backend attached.
    make_judge(Settings(judge_provider="anthropic", anthropic_api_key="sk-a"))
    assert built["embedder"] is None

    # OpenAI key present: an OpenAI-backed embedder is attached to the Anthropic judge.
    built.clear()
    make_judge(
        Settings(
            judge_provider="anthropic",
            anthropic_api_key="sk-a",
            openai_api_key="sk-o",
            openai_base_url="http://localhost:1234/v1",
        )
    )
    assert isinstance(built["embedder"], DummyOpenAI)
    assert built["embedder"].kwargs["base_url"] == "http://localhost:1234/v1"


def test_make_judge_agent_needs_no_key_and_wires_an_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict = {}

    class DummyAgent:
        def __init__(self, **kwargs) -> None:
            built.update(**kwargs)

    class DummyOpenAI:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs  # stands in for the embedder backend

    monkeypatch.setattr(judges_pkg, "AgentJudge", DummyAgent)
    monkeypatch.setattr(judges_pkg, "OpenAIJudge", DummyOpenAI)

    # No API key of any kind: the agent judge authenticates through the Claude Code
    # session, so the factory must not gate on one — and with no OpenAI key there is
    # no embeddings backend to attach.
    make_judge(
        Settings(
            judge_provider="agent",
            agent_judge_model="claude-opus-4-8",
            claude_code_oauth_token=None,
        )
    )
    assert built["model"] == "claude-opus-4-8"
    assert built["embedder"] is None
    assert built["oauth_token"] is None

    # CLAUDE_CODE_OAUTH_TOKEN reaches the judge, which is what authenticates it where
    # there is no Claude Code session (Docker). A blank value means "unset".
    built.clear()
    make_judge(Settings(judge_provider="agent", claude_code_oauth_token="sk-ant-oat-x"))
    assert built["oauth_token"] == "sk-ant-oat-x"

    built.clear()
    make_judge(Settings(judge_provider="agent", claude_code_oauth_token="  "))
    assert built["oauth_token"] is None

    built.clear()
    make_judge(
        Settings(
            judge_provider="agent",
            openai_api_key="sk-o",
            openai_base_url=None,
        )
    )
    assert isinstance(built["embedder"], DummyOpenAI)
    assert built["embedder"].kwargs["base_url"] is None

    # OPENAI_BASE_URL reaches the embedder, so a key-free agent run can embed against a
    # local OpenAI-compatible server instead of api.openai.com.
    built.clear()
    make_judge(
        Settings(
            judge_provider="agent",
            openai_api_key="local",
            openai_base_url="http://localhost:1234/v1",
        )
    )
    assert built["embedder"].kwargs["base_url"] == "http://localhost:1234/v1"


def test_make_judge_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        make_judge(Settings(judge_provider="anthropic", anthropic_api_key=None))
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make_judge(Settings(judge_provider="openai", openai_api_key=None))


def test_make_judge_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="desconocido"):
        make_judge(Settings(judge_provider="gemini"))


def test_make_judge_builds_step_models_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict = {}

    class DummyAnthropic:
        def __init__(self, **kwargs) -> None:
            built.update(**kwargs)

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)

    make_judge(
        Settings(
            judge_provider="anthropic",
            anthropic_api_key="sk-a",
            anthropic_judge_model="claude-strong",
            judge_extract_model="claude-cheap",
            # judge_score_model left unset → SCORE uses the default judge model.
        )
    )
    step_models = built["step_models"]
    assert step_models.for_step(JudgeStep.EXTRACT) == "claude-cheap"
    # No VERIFY config knob → VERIFY falls back to the default judge model.
    assert step_models.for_step(JudgeStep.VERIFY) == "claude-strong"
    assert step_models.for_step(JudgeStep.SCORE) == "claude-strong"
    assert step_models.for_step(None) == "claude-strong"


# --- Token-usage recording ----------------------------------------------------


def test_anthropic_records_input_output_usage(turn: TurnView) -> None:
    # score() and structured() each record the response's input/output tokens onto
    # the active accumulator.
    client = FakeAnthropicClient(
        _ScoreResponse(score=3.0, justification="ok"),
        usage=SimpleNamespace(input_tokens=12, output_tokens=5),
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)
    acc = UsageAccumulator()
    with collect_usage(acc):
        judge.score(rubric="r", turn=turn, scale=Likert())
    snap = acc.snapshot()
    assert (snap.input_tokens, snap.output_tokens) == (12, 5)
    assert snap.total_tokens == 17


def test_anthropic_without_usage_records_nothing(turn: TurnView) -> None:
    # A response with no `usage` (the default fake) contributes 0 — getattr-safe.
    client = FakeAnthropicClient(_ScoreResponse(score=3.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)
    acc = UsageAccumulator()
    with collect_usage(acc):
        judge.score(rubric="r", turn=turn, scale=Likert())
    assert acc.snapshot() == UsageAccumulator().snapshot()  # still 0/0


def test_openai_maps_prompt_completion_to_input_output(turn: TurnView) -> None:
    client = FakeOpenAIClient(
        _ScoreResponse(score=1.0, justification="ok"),
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=8),
    )
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)
    acc = UsageAccumulator()
    with collect_usage(acc):
        judge.score(rubric="r", turn=turn, scale=Boolean())
    snap = acc.snapshot()
    assert (snap.input_tokens, snap.output_tokens) == (20, 8)  # prompt→input, completion→output


def test_openai_embed_records_prompt_tokens_as_input() -> None:
    client = FakeOpenAIClient(
        None, embeddings=[[1.0, 0.0]], embed_usage=SimpleNamespace(prompt_tokens=7)
    )
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)
    acc = UsageAccumulator()
    with collect_usage(acc):
        judge.embed(texts=["a"])
    snap = acc.snapshot()
    assert (snap.input_tokens, snap.output_tokens) == (7, 0)  # embeddings have no output side


def test_usage_not_recorded_outside_a_collect_scope(turn: TurnView) -> None:
    # Without an active accumulator, recording is a silent no-op (judges stay usable
    # standalone). The call still succeeds.
    client = FakeAnthropicClient(
        _ScoreResponse(score=2.0, justification="ok"),
        usage=SimpleNamespace(input_tokens=9, output_tokens=3),
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)
    verdict = judge.score(rubric="r", turn=turn, scale=Likert())  # no collect_usage
    assert verdict.score == 2.0


# --- Per-call prompt recording ------------------------------------------------
# The judges keep no memory of the text they send, so the recorder is the only place
# a score's exact prompt survives. Same ambient shape as the usage accumulator.


def test_anthropic_records_the_prompt_of_each_call(turn: TurnView) -> None:
    client = FakeAnthropicClient(
        _ScoreResponse(score=3.0, justification="ok"),
        usage=SimpleNamespace(input_tokens=12, output_tokens=5),
    )
    judge = AnthropicJudge(
        model="claude-opus-4-8", client=client, system_prompt="Eres un juez."
    )
    recorder = CallRecorder()
    with collect_calls(recorder):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert(), step=JudgeStep.SCORE)

    (call,) = recorder.snapshot()
    assert call.step == "score"
    assert call.model == "claude-opus-4-8"
    assert call.system_prompt == "Eres un juez."
    # The record carries exactly the user content the client was handed.
    assert call.prompt == client.messages.calls[0]["messages"][0]["content"]
    assert turn.prompt in call.prompt
    # ``record_usage`` attributes the response's tokens to the open call, not only to
    # the turn accumulator, so the judges need no per-call bookkeeping of their own.
    assert (call.input_tokens, call.output_tokens) == (12, 5)


def test_openai_structured_records_its_rendered_instruction(turn: TurnView) -> None:
    client = FakeOpenAIClient(
        Claims(claims=["a"]), usage=SimpleNamespace(prompt_tokens=20, completion_tokens=8)
    )
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)
    recorder = CallRecorder()
    with collect_calls(recorder):
        judge.structured(
            instruction="Extrae", turn=turn, schema=Claims, step=JudgeStep.EXTRACT
        )

    (call,) = recorder.snapshot()
    assert call.step == "extract"
    assert call.model == "gpt-5.6-sol"
    assert call.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert call.prompt == render_prompt("Extrae", turn)
    assert (call.input_tokens, call.output_tokens) == (20, 8)


def test_agent_records_one_entry_per_call_in_order(turn: TurnView) -> None:
    client = FakeAgentSdk({"score": 3.0, "justification": "ok"})
    judge = AgentJudge(model="claude-opus-4-8", client=client)
    recorder = CallRecorder()
    with collect_calls(recorder):
        judge.score(rubric="Primera", turn=turn, scale=Likert())
        judge.score(rubric="Segunda", turn=turn, scale=Likert())

    calls = recorder.snapshot()
    assert len(calls) == 2
    assert calls[0].prompt.startswith("Primera")
    assert calls[1].prompt.startswith("Segunda")
    assert all(c.step is None for c in calls)  # no step given → none recorded


def test_a_failed_call_is_still_recorded(turn: TurnView) -> None:
    # The prompt was sent and paid for; a failing call is precisely the one worth
    # reading back, so the record is appended even though score() raised.
    client = FakeAnthropicClient(None, raises=RuntimeError("caída"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)
    recorder = CallRecorder()
    with collect_calls(recorder), pytest.raises(JudgeError):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    (call,) = recorder.snapshot()
    assert call.prompt.startswith("Evalúa")


def test_calls_not_recorded_outside_a_collect_scope(turn: TurnView) -> None:
    # Without an active recorder, recording is a silent no-op — judges stay usable
    # standalone, exactly like the usage accumulator.
    client = FakeAnthropicClient(_ScoreResponse(score=2.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    verdict = judge.score(rubric="r", turn=turn, scale=Likert())  # no collect_calls

    assert verdict.score == 2.0
    assert CallRecorder().snapshot() == []


# --- Retry / backoff on provider throttling -----------------------------------
# The between-turn pause is per-process, so it cannot bound the aggregate request rate
# once a run spreads across workers and concurrent metrics. ``judge_call`` is the
# reactive half: each call backs off from the throttling it personally sees.


@pytest.fixture
def backoff_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff waits instead of sleeping, with jitter pinned to its ceiling.

    Patching ``base._sleep`` (an indirection that exists for exactly this) keeps the real
    ``time.sleep`` untouched, and pinning ``random.uniform`` to the top of the window
    makes the exponential growth assertable.
    """
    waits: list[float] = []
    monkeypatch.setattr(judges_base, "_sleep", waits.append)
    monkeypatch.setattr(judges_base.random, "uniform", lambda low, high: high)
    return waits


@pytest.fixture
def set_policy(monkeypatch: pytest.MonkeyPatch):
    """Pin the retry policy, so these tests do not depend on ambient settings/.env."""

    def _set(max_attempts: int, base_seconds: float = 1.0, max_seconds: float = 60.0) -> None:
        policy = RetryPolicy(max_attempts, base_seconds, max_seconds)
        monkeypatch.setattr(judges_base, "retry_policy", lambda: policy)

    return _set


def test_throttled_call_is_retried_then_succeeds(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    set_policy(max_attempts=3)
    client = FakeAnthropicClient(
        _ScoreResponse(score=4.0, justification="bien"),
        raises=[throttled(429), None],  # throttled once, then fine
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    verdict = judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert verdict.score == 4.0
    assert len(client.messages.calls) == 2  # the retry really re-issued the call
    assert backoff_waits == [1.0]


def test_retries_are_exhausted_and_the_failure_is_wrapped(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    set_policy(max_attempts=4)
    client = FakeAnthropicClient(None, raises=throttled(429))  # throttled forever
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError) as exc_info:
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert len(client.messages.calls) == 4  # max_attempts counts the first try
    assert backoff_waits == [1.0, 2.0, 4.0]  # exponential, one wait per retry
    assert "claude-opus-4-8" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None


def test_backoff_is_clamped_to_the_configured_ceiling(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    # Without a ceiling the window doubles without bound and one judge call could hold
    # its worker thread for hours.
    set_policy(max_attempts=5, base_seconds=1.0, max_seconds=3.0)
    client = FakeAnthropicClient(None, raises=throttled(429))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert backoff_waits == [1.0, 2.0, 3.0, 3.0]


@pytest.mark.parametrize("status", [429, 503, 529])
def test_every_capacity_status_is_retried(
    turn: TurnView, backoff_waits: list[float], set_policy, status: int
) -> None:
    # 429 rate limit, 503 unavailable, 529 Anthropic overloaded_error — same failure
    # class, same correct response.
    set_policy(max_attempts=2)
    client = FakeAnthropicClient(
        _ScoreResponse(score=1.0, justification="ok"), raises=[throttled(status), None]
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    assert judge.score(rubric="Evalúa", turn=turn, scale=Likert()).score == 1.0
    assert len(client.messages.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(throttled(400), id="bad-request"),
        pytest.param(throttled(401), id="auth"),
        # Classification is by status_code, not message text: an unrelated error that
        # merely mentions a rate limit must stay permanent.
        pytest.param(RuntimeError("429 rate limit"), id="no-status-code"),
    ],
)
def test_permanent_failures_are_not_retried(
    turn: TurnView, backoff_waits: list[float], set_policy, failure: Exception
) -> None:
    set_policy(max_attempts=5)
    client = FakeAnthropicClient(None, raises=failure)
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert len(client.messages.calls) == 1  # failed once, gave up
    assert backoff_waits == []


def test_retry_after_header_wins_over_computed_backoff(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    set_policy(max_attempts=2, base_seconds=1.0, max_seconds=60.0)
    client = FakeAnthropicClient(
        _ScoreResponse(score=3.0, justification="ok"),
        raises=[throttled(429, retry_after="30"), None],
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert backoff_waits == [30.0]  # the provider's own number, not the 1.0 window


def test_retry_after_is_clamped_to_the_configured_ceiling(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    set_policy(max_attempts=2, base_seconds=1.0, max_seconds=60.0)
    client = FakeAnthropicClient(
        _ScoreResponse(score=3.0, justification="ok"),
        raises=[throttled(429, retry_after="900"), None],
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert backoff_waits == [60.0]


def test_unparseable_retry_after_falls_back_to_computed_backoff(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    # Retry-After may be an HTTP-date rather than seconds; that must not blow up.
    set_policy(max_attempts=2, base_seconds=1.0)
    client = FakeAnthropicClient(
        _ScoreResponse(score=3.0, justification="ok"),
        raises=[throttled(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), None],
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert backoff_waits == [1.0]


def test_a_single_attempt_disables_retrying(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    set_policy(max_attempts=1)
    client = FakeAnthropicClient(None, raises=throttled(429))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    with pytest.raises(JudgeError):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    assert len(client.messages.calls) == 1
    assert backoff_waits == []


def test_openai_score_and_embed_share_the_retry_seam(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    # Both OpenAI entry points route through judge_call, so both retry.
    set_policy(max_attempts=2)
    client = FakeOpenAIClient(
        _ScoreResponse(score=2.0, justification="ok"),
        embeddings=[[1.0, 0.0]],
        raises=[throttled(429), None],
        embed_raises=[throttled(503), None],
    )
    judge = OpenAIJudge(model="gpt-5.6-sol", client=client)

    assert judge.score(rubric="Evalúa", turn=turn, scale=Likert()).score == 2.0
    assert judge.embed(texts=["a"]) == [[1.0, 0.0]]
    assert len(client.chat.completions.calls) == 2
    assert len(client.embeddings.calls) == 2


def test_usage_is_recorded_once_from_the_successful_attempt(
    turn: TurnView, backoff_waits: list[float], set_policy
) -> None:
    # A throttled attempt spends no tokens, and usage is read after judge_call returns —
    # so a retried call must not double-count.
    set_policy(max_attempts=3)
    client = FakeAnthropicClient(
        _ScoreResponse(score=2.0, justification="ok"),
        raises=[throttled(429), None],
        usage=SimpleNamespace(input_tokens=10, output_tokens=4),
    )
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)
    acc = UsageAccumulator()
    with collect_usage(acc):
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())

    snap = acc.snapshot()
    assert (snap.input_tokens, snap.output_tokens) == (10, 4)


def test_retry_policy_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judges_base,
        "get_settings",
        lambda: Settings(
            judge_retry_max_attempts=7,
            judge_retry_base_seconds=2.5,
            judge_retry_max_seconds=90.0,
        ),
    )
    assert judges_base.retry_policy() == RetryPolicy(7, 2.5, 90.0)


def test_retry_policy_clamps_nonsensical_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # max_attempts < 1 would make judge_call skip the call entirely rather than disable
    # retrying, and negative waits are meaningless.
    monkeypatch.setattr(
        judges_base,
        "get_settings",
        lambda: Settings(
            judge_retry_max_attempts=0,
            judge_retry_base_seconds=-1.0,
            judge_retry_max_seconds=-1.0,
        ),
    )
    assert judges_base.retry_policy() == RetryPolicy(1, 0.0, 0.0)


def test_anthropic_owns_the_claude_5_family(turn: TurnView) -> None:
    """The allow-list gates before the SDK, so an unlisted model never reaches it."""
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-4-8", client=client)

    for model in ("claude-fable-5", "claude-opus-5", "claude-sonnet-5"):
        assert judge.resolve_model(None, model) == model

    with pytest.raises(ValueError):
        judge.resolve_model(None, "claude-opus-6")


def test_agent_judge_shares_the_anthropic_allow_list() -> None:
    """One list, two judges: the Agent judge calls the same Claude models."""
    from scorekeeper.core.metrics.judges import agent_judge, anthropic_judge

    assert agent_judge.KNOWN_MODELS is anthropic_judge.KNOWN_MODELS
    assert agent_judge.ADAPTIVE_THINKING_MODELS is anthropic_judge.ADAPTIVE_THINKING_MODELS


# -- server-side refusal fallback -------------------------------------------------------------


def _fallback_judge(client: FakeAnthropicClient, **kwargs) -> AnthropicJudge:
    return AnthropicJudge(
        model="claude-opus-4-8", client=client, fallback_model="claude-opus-4-8", **kwargs
    )


def test_refusal_fallback_is_sent_only_for_the_models_that_accept_it(
    turn: TurnView,
) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = _fallback_judge(client)

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-opus-5")
    call = client.beta.messages.calls[-1]
    assert call["fallbacks"] == [{"model": "claude-opus-4-8"}]
    assert call["betas"] == ["server-side-fallback-2026-06-01"]

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-fable-5")
    assert client.beta.messages.calls[-1]["fallbacks"] == [{"model": "claude-opus-4-8"}]

    # Sonnet 5 and Opus 4.8 do not take the parameter: sending it would be a 400 on
    # every call, so they stay on the plain namespace untouched.
    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-sonnet-5")
    assert "fallbacks" not in client.messages.calls[-1]
    assert len(client.beta.messages.calls) == 2


def test_refusal_fallback_never_falls_back_to_the_refusing_model(turn: TurnView) -> None:
    """Retrying on the model that just declined would only buy a second refusal."""
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(
        model="claude-opus-5", client=client, fallback_model="claude-opus-5"
    )

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-opus-5")

    assert client.beta.messages.calls == []
    assert "fallbacks" not in client.messages.calls[-1]


def test_without_a_fallback_model_the_request_is_unchanged(turn: TurnView) -> None:
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    judge = AnthropicJudge(model="claude-opus-5", client=client)  # no fallback_model

    judge.score(rubric="s", turn=turn, scale=Unit(), model="claude-opus-5")

    assert client.beta.messages.calls == []
    assert "fallbacks" not in client.messages.calls[-1]
    assert "betas" not in client.messages.calls[-1]


def test_an_unowned_fallback_model_fails_when_the_judge_is_built() -> None:
    """A configuration typo should not wait for the first refusal to surface."""
    client = FakeAnthropicClient(_ScoreResponse(score=1.0, justification="ok"))
    with pytest.raises(ValueError):
        AnthropicJudge(model="claude-opus-5", client=client, fallback_model="gpt-4o")


def test_the_fallback_also_covers_structured_calls(turn: TurnView) -> None:
    client = FakeAnthropicClient(Claims(claims=["a"]))
    judge = _fallback_judge(client)

    judge.structured(instruction="x", turn=turn, schema=Claims, model="claude-fable-5")

    assert client.beta.messages.calls[-1]["fallbacks"] == [{"model": "claude-opus-4-8"}]


def test_make_judge_passes_the_configured_fallback_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict = {}

    class DummyAnthropic:
        def __init__(self, **kwargs) -> None:
            built.update(**kwargs)

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)

    make_judge(
        Settings(
            judge_provider="anthropic",
            anthropic_api_key="sk-a",
            judge_fallback_model="claude-sonnet-5",
        )
    )
    assert built["fallback_model"] == "claude-sonnet-5"

    built.clear()
    make_judge(
        Settings(
            judge_provider="anthropic",
            anthropic_api_key="sk-a",
            judge_fallback_model=None,
        )
    )
    assert built["fallback_model"] is None
