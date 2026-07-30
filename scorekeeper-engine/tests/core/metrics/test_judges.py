"""Offline tests for the real judges — fake SDK clients, no network, no LLM SDK.

Each judge takes an injected ``client``, so these run without ``anthropic`` or
``openai`` installed (the SDKs are imported only when a judge builds its own
client). We assert on prompt construction, score clamping, Spanish-justification
passthrough, ``structured()`` return type, and factory selection/errors.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from scorekeeper.config.settings import Settings
from scorekeeper.core.metrics.base import TurnView
from scorekeeper.core.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.core.metrics import judges as judges_pkg
from scorekeeper.core.metrics.judges import (
    AnthropicJudge,
    LMStudioJudge,
    OpenAIJudge,
    make_judge,
)
from scorekeeper.core.metrics.judges import base as judges_base
from scorekeeper.core.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    JudgeError,
    RetryPolicy,
    StepModels,
    UsageAccumulator,
    _ScoreResponse,
    clamp,
    collect_usage,
    render_prompt,
    scale_spec,
)
from scorekeeper.core.metrics.scale import Boolean, Likert, Unit
from scorekeeper.core.retrieved_context import RetrievedContext, RetrievedDocument


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
    def __init__(
        self,
        parsed: object,
        raises: Exception | list[Exception | None] | None = None,
        usage: object | None = None,
    ) -> None:
        self.messages = FakeAnthropicMessages(parsed, raises, usage)


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
                    content="Devoluciones en 30 días. Requiere recibo.",
                    url="https://ejemplo.com/manual",
                )
            ]
        ),
    )
    rendered = render_prompt("Evalúa la fidelidad.", turn)

    assert "--- Contexto recuperado ---" in rendered
    # The rendered context surfaces the content plus its label and source metadata.
    assert "Devoluciones en 30 días. Requiere recibo." in rendered
    assert "Política de devoluciones" in rendered
    assert "manual.pdf" in rendered
    assert "https://ejemplo.com/manual" in rendered


def test_render_prompt_substitutes_context_placeholder() -> None:
    turn = TurnView(
        prompt="p",
        response="r",
        retrieved_context=RetrievedContext.from_blob("pasaje A\npasaje B"),
    )
    rendered = render_prompt("Contexto: {context}", turn)

    # The {context} placeholder expands to the rendered retrieved context.
    assert "Contexto: pasaje A\npasaje B" in rendered


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
                    name="Política", document="", content="", url="https://a/pol"
                ),
                RetrievedDocument(
                    name="", document="", content="", url="https://b/x"
                ),
            ]
        ),
    )
    rendered = render_prompt("Contexto: {context}", turn)

    assert "--- Contexto recuperado ---" in rendered
    assert "Política" in rendered
    assert "https://a/pol" in rendered
    assert "https://b/x" in rendered
    assert '{"name"' not in rendered  # raw JSON never reaches the judge.


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


def test_lmstudio_errors_name_its_own_provider(turn: TurnView) -> None:
    # LMStudioJudge inherits OpenAIJudge's call methods; its descriptive errors must
    # report "LM Studio", not the inherited "OpenAI", so the failing backend is clear.
    client = FakeOpenAIClient(None)
    judge = LMStudioJudge(model="local-model", client=client)

    with pytest.raises(JudgeError) as exc_info:
        judge.score(rubric="Evalúa", turn=turn, scale=Likert())
    message = str(exc_info.value)
    assert "LM Studio" in message
    assert "OpenAI" not in message


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
            pass  # stands in for the embedder backend

    monkeypatch.setattr(judges_pkg, "AnthropicJudge", DummyAnthropic)
    monkeypatch.setattr(judges_pkg, "OpenAIJudge", DummyOpenAI)

    # No OpenAI key: no embeddings backend attached.
    make_judge(Settings(judge_provider="anthropic", anthropic_api_key="sk-a"))
    assert built["embedder"] is None

    # OpenAI key present: an OpenAI-backed embedder is attached to the Anthropic judge.
    built.clear()
    make_judge(
        Settings(
            judge_provider="anthropic", anthropic_api_key="sk-a", openai_api_key="sk-o"
        )
    )
    assert isinstance(built["embedder"], DummyOpenAI)


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
    # Both OpenAI entry points route through judge_call, so both retry (and LMStudioJudge
    # inherits them unchanged).
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
