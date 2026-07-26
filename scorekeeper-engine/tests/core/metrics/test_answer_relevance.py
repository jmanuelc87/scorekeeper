"""AnswerRelevance: reverse-generate questions, embed, average cosine.

Tested with no LLM and no DB: the StubJudge scripts the generated questions and a
text->vector embedding table. The metric is imported directly from the catalog so
these tests are independent of the isolated ``registered_metrics`` registry.
"""

from __future__ import annotations

import pytest

from scorekeeper.core.metrics.base import TurnView
from scorekeeper.core.metrics.catalog.answer_relevance import (
    AnswerRelevance,
    cosine_similarity,
)


def _questions(*questions: str):
    """Scripted GeneratedQuestion extractions for the stub judge."""
    from scorekeeper.core.metrics.catalog.answer_relevance import GeneratedQuestion

    return [GeneratedQuestion(question=q) for q in questions]


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
    metric = AnswerRelevance()
    metric.n_questions = 2  # instance override keeps the test small

    result = metric.evaluate(turn, judge)

    assert result.metric_name == "answer_relevance"
    assert result.raw_score == pytest.approx(1.0)
    assert result.normalized_score == pytest.approx(1.0)  # Unit scale: identity
    assert result.judge_model == "claude-x"
    # Call order: n generation calls, then a single batched embed call.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured", "embed"]
    # The embed call batched the original question + both generated questions.
    assert judge.calls[-1] == ("embed", "3")
    # Structured trace: generation step, similarity step, mean step.
    labels = [step.label for step in result.trace.steps]
    assert "Preguntas generadas a partir de la respuesta" in labels
    assert "Relevancia media" in labels


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
    metric = AnswerRelevance()
    metric.n_questions = 2

    result = metric.evaluate(turn, judge)

    # (1.0 + 0.0) / 2
    assert result.raw_score == pytest.approx(0.5)
    # Similarities are typed entry values, not formatted strings.
    sim_step = next(s for s in result.trace.steps if s.label == "Similitud por pregunta")
    assert {e.label: e.value for e in sim_step.entries} == {"igual": 1.0, "ortogonal": 0.0}


def test_negative_cosine_is_clamped_to_zero(make_judge) -> None:
    turn = TurnView(prompt="q", response="respuesta")
    judge = make_judge(
        extractions=_questions("opuesta"),
        embeddings={"q": [1.0, 0.0], "opuesta": [-1.0, 0.0]},  # cosine -1.0
    )
    metric = AnswerRelevance()
    metric.n_questions = 1

    result = metric.evaluate(turn, judge)

    # Mean cosine is -1.0 but the raw score is clamped into the Unit range.
    assert result.raw_score == 0.0


def test_no_questions_generated_is_safe(make_judge) -> None:
    turn = TurnView(prompt="q", response="respuesta")
    # The model returns only blank questions, which are filtered out.
    judge = make_judge(extractions=_questions("", "   "))
    metric = AnswerRelevance()
    metric.n_questions = 2

    result = metric.evaluate(turn, judge)

    assert result.raw_score == 0.0
    # No embedding call happens when there is nothing to compare.
    assert [kind for kind, _ in judge.calls] == ["structured", "structured"]
    # No questions → generation step has no entries; relevance is zero.
    gen_step = result.trace.steps[0]
    assert gen_step.entries == []
    assert result.trace.steps[-1].entries[0].value == 0.0


def test_generation_step_does_not_leak_original_question(make_judge) -> None:
    turn = TurnView(prompt="PREGUNTA_SECRETA", response="respuesta")
    judge = make_judge(
        extractions=_questions("p"),
        embeddings={"PREGUNTA_SECRETA": [1.0], "p": [1.0]},
    )

    seen_prompts: list[str] = []
    original_structured = judge.structured

    def _spy(*, instruction, turn, schema, step=None, model=None):  # noqa: A002 - mirror protocol kwarg name
        seen_prompts.append(turn.prompt)
        return original_structured(
            instruction=instruction, turn=turn, schema=schema, step=step, model=model
        )

    judge.structured = _spy
    metric = AnswerRelevance()
    metric.n_questions = 1
    metric.evaluate(turn, judge)

    # The generator sees a response-only turn, never the original question.
    assert seen_prompts == [""]
