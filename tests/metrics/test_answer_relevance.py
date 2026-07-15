"""RelevanciaRespuesta: reverse-generate questions, embed, average cosine.

Tested with no LLM and no DB: the StubJudge scripts the generated questions and a
text->vector embedding table. The metric is imported directly from the catalog so
these tests are independent of the isolated ``registered_metrics`` registry.
"""

from __future__ import annotations

import pytest

from scorekeeper.metrics.base import TurnView
from scorekeeper.metrics.catalog.relevancia_respuesta import (
    RelevanciaRespuesta,
    cosine_similarity,
)


def _questions(*preguntas: str):
    """Scripted PreguntaGenerada extractions for the stub judge."""
    from scorekeeper.metrics.catalog.relevancia_respuesta import PreguntaGenerada

    return [PreguntaGenerada(pregunta=p) for p in preguntas]


def test_cosine_similarity_basic() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # Degenerate inputs never raise.
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_perfect_relevance_when_questions_match_original(make_judge) -> None:
    turn = TurnView(prompt="¿Cómo reinicio el router?", response="Mantén pulsado 10s.")
    # Original question and all reverse-generated questions share the same vector.
    vec = [1.0, 0.0, 0.0]
    judge = make_judge(
        extractions=_questions("¿Cómo reinicio el router?", "¿Cómo reinicio el router?"),
        embeddings={
            "¿Cómo reinicio el router?": vec,
        },
        model="claude-x",
    )
    metric = RelevanciaRespuesta()
    metric.n_questions = 2  # instance override keeps the test small

    result = metric.evaluate(turn, judge)

    assert result.metric_name == "relevancia_respuesta"
    assert result.raw_score == pytest.approx(1.0)
    assert result.normalized_score == pytest.approx(1.0)  # Unit scale: identity
    assert result.judge_model == "claude-x"
    # Call order: n generation calls, then a single batched embed call.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured", "embed"]
    # The embed call batched the original question + both generated questions.
    assert judge.calls[-1] == ("embed", "3")
    # Every step is flattened into the Spanish justification.
    assert "### Preguntas generadas a partir de la respuesta" in result.justification
    assert "### Relevancia media" in result.justification


def test_partial_relevance_is_averaged(make_judge) -> None:
    turn = TurnView(prompt="q", response="respuesta")
    judge = make_judge(
        extractions=_questions("igual", "ortogonal"),
        embeddings={
            "q": [1.0, 0.0],
            "igual": [1.0, 0.0],  # cosine 1.0 with q
            "ortogonal": [0.0, 1.0],  # cosine 0.0 with q
        },
    )
    metric = RelevanciaRespuesta()
    metric.n_questions = 2

    result = metric.evaluate(turn, judge)

    # (1.0 + 0.0) / 2
    assert result.raw_score == pytest.approx(0.5)
    assert "coseno = 1.000" in result.justification
    assert "coseno = 0.000" in result.justification


def test_negative_cosine_is_clamped_to_zero(make_judge) -> None:
    turn = TurnView(prompt="q", response="respuesta")
    judge = make_judge(
        extractions=_questions("opuesta"),
        embeddings={"q": [1.0, 0.0], "opuesta": [-1.0, 0.0]},  # cosine -1.0
    )
    metric = RelevanciaRespuesta()
    metric.n_questions = 1

    result = metric.evaluate(turn, judge)

    # Mean cosine is -1.0 but the raw score is clamped into the Unit range.
    assert result.raw_score == 0.0


def test_no_questions_generated_is_safe(make_judge) -> None:
    turn = TurnView(prompt="q", response="respuesta")
    # The model returns only blank questions, which are filtered out.
    judge = make_judge(extractions=_questions("", "   "))
    metric = RelevanciaRespuesta()
    metric.n_questions = 2

    result = metric.evaluate(turn, judge)

    assert result.raw_score == 0.0
    # No embedding call happens when there is nothing to compare.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    assert "(ninguna)" in result.justification


def test_generation_step_does_not_leak_original_question(make_judge) -> None:
    turn = TurnView(prompt="PREGUNTA_SECRETA", response="respuesta")
    judge = make_judge(
        extractions=_questions("p"),
        embeddings={"PREGUNTA_SECRETA": [1.0], "p": [1.0]},
    )

    seen_prompts: list[str] = []
    original_structured = judge.structured

    def _spy(*, instruction, turn, schema):  # noqa: A002 - mirror protocol kwarg name
        seen_prompts.append(turn.prompt)
        return original_structured(instruction=instruction, turn=turn, schema=schema)

    judge.structured = _spy
    metric = RelevanciaRespuesta()
    metric.n_questions = 1
    metric.evaluate(turn, judge)

    # The generator sees a response-only turn, never the original question.
    assert seen_prompts == [""]
