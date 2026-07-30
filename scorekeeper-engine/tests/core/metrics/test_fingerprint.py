"""Tests for the scoring fingerprint — pure functions, no DB and no judge.

Every case is the same shape: hash a baseline, change exactly one input, and assert
the key moved (or, for the stability case, that it did not). What must invalidate a
stored score is the whole contract of this module, so each input gets its own case.
"""

from __future__ import annotations

import pytest

from scorekeeper.core.metrics.base import Metric, MetricResult, TurnView
from scorekeeper.core.metrics.category import MetricCategory
from scorekeeper.core.metrics.fingerprint import scoring_key
from scorekeeper.core.metrics.scale import Unit
from scorekeeper.core.retrieved_context import RetrievedContext, RetrievedDocument


class Utilidad(Metric):
    name = "utilidad"
    category = MetricCategory.RAG
    scale = Unit()

    def evaluate(self, turn: TurnView, judge) -> MetricResult:  # pragma: no cover - unused
        raise NotImplementedError


VERSIONS = {"rubrica": "11111111-1111-1111-1111-111111111111"}
MODELS = {"extract": "modelo-x", "verify": "modelo-x", "score": "modelo-y"}


def _view(**overrides) -> TurnView:
    defaults = dict(
        prompt="¿Cómo renuevo mi póliza?",
        response="Entra al portal y pulsa Renovar.",
        turn_number=2,
        history=[("hola", "buenas")],
        retrieved_context=RetrievedContext(
            documents=[
                RetrievedDocument(name="a", document="d1.pdf", content="uno", url=None),
                RetrievedDocument(name="b", document="d2.pdf", content="dos", url=None),
            ]
        ),
        expected_output="Renovar desde el portal.",
    )
    return TurnView(**{**defaults, **overrides})


def _key(metric: Metric | None = None, view: TurnView | None = None, **overrides) -> str:
    return scoring_key(
        metric or Utilidad(),
        view or _view(),
        prompt_versions=overrides.get("prompt_versions", VERSIONS),
        judge_models=overrides.get("judge_models", MODELS),
    )


def test_key_is_stable_for_identical_inputs() -> None:
    # Fresh objects each time: the key must depend on the values, not on identity.
    assert _key() == _key()


def test_key_is_64_hex_chars() -> None:
    # Pins the MetricScore.scoring_key String(64) contract.
    key = _key()
    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")


class _OtraRubrica(Utilidad):
    rubric_version = "v2"


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(
            lambda: _key(metric=_OtraRubrica()),
            id="rubric_version",
        ),
        pytest.param(
            lambda: _key(prompt_versions={"rubrica": "22222222-2222-2222-2222-222222222222"}),
            id="prompt_version",
        ),
        pytest.param(
            lambda: _key(judge_models={**MODELS, "score": "modelo-z"}),
            id="judge_model",
        ),
        pytest.param(lambda: _key(view=_view(prompt="otra pregunta")), id="prompt"),
        pytest.param(lambda: _key(view=_view(response="otra respuesta")), id="response"),
        pytest.param(lambda: _key(view=_view(turn_number=3)), id="turn_number"),
        pytest.param(lambda: _key(view=_view(history=[])), id="history"),
        pytest.param(
            lambda: _key(view=_view(expected_output="otra cosa")), id="expected_output"
        ),
        pytest.param(
            lambda: _key(
                view=_view(
                    retrieved_context=RetrievedContext(
                        documents=[
                            RetrievedDocument(
                                name="a", document="d1.pdf", content="UNO", url=None
                            ),
                            RetrievedDocument(
                                name="b", document="d2.pdf", content="dos", url=None
                            ),
                        ]
                    )
                )
            ),
            id="context_content",
        ),
        pytest.param(
            lambda: _key(
                view=_view(
                    retrieved_context=RetrievedContext(
                        documents=[
                            RetrievedDocument(
                                name="b", document="d2.pdf", content="dos", url=None
                            ),
                            RetrievedDocument(
                                name="a", document="d1.pdf", content="uno", url=None
                            ),
                        ]
                    )
                )
            ),
            # Documents are ordered by retriever rank and contextual_precision scores
            # that order, so a reordering is a different evaluation.
            id="context_order",
        ),
    ],
)
def test_key_changes_when_one_input_changes(changed) -> None:
    assert changed() != _key()
