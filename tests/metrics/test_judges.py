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

from scorekeeper.config import Settings
from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.judge import JudgeStep, JudgeVerdict
from scorekeeper.metrics import judges as judges_pkg
from scorekeeper.metrics.judges import (
    AnthropicJudge,
    LMStudioJudge,
    OpenAIJudge,
    make_judge,
)
from scorekeeper.metrics.judges.base import (
    DEFAULT_SYSTEM_PROMPT,
    JudgeError,
    StepModels,
    UsageAccumulator,
    _ScoreResponse,
    clamp,
    collect_usage,
    render_prompt,
    scale_spec,
)
from scorekeeper.metrics.scale import Boolean, Likert, Unit
from scorekeeper.retrieved_context import RetrievedContext, RetrievedDocument


class Claims(BaseModel):
    claims: list[str] = []
    summary: str = ""


# --- Fake SDK clients ---------------------------------------------------------


class FakeAnthropicMessages:
    def __init__(
        self,
        parsed: object,
        raises: Exception | None = None,
        usage: object | None = None,
    ) -> None:
        self._parsed = parsed
        self._raises = raises
        self._usage = usage
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
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
        raises: Exception | None = None,
        usage: object | None = None,
    ) -> None:
        self.messages = FakeAnthropicMessages(parsed, raises, usage)


class FakeCompletions:
    def __init__(
        self,
        parsed: object,
        refusal: str | None = None,
        usage: object | None = None,
    ) -> None:
        self._parsed = parsed
        self._refusal = refusal
        self._usage = usage
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        message = type("Msg", (), {"parsed": self._parsed, "refusal": self._refusal})()
        choice = type("Choice", (), {"message": message})()
        attrs: dict[str, object] = {"choices": [choice]}
        if self._usage is not None:
            attrs["usage"] = self._usage
        return type("Completion", (), attrs)()


class FakeEmbeddings:
    def __init__(self, vectors: list[list[float]], usage: object | None = None) -> None:
        self._vectors = vectors
        self._usage = usage
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
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
    ) -> None:
        self.chat = FakeChat(FakeCompletions(parsed, refusal, usage))
        self.embeddings = FakeEmbeddings(embeddings or [], embed_usage)


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
