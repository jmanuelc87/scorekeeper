"""Tests for the shared syntok sentence segmenter."""

from __future__ import annotations

import pytest

from scorekeeper.core.text import split_sentences


def test_splits_spanish_sentences() -> None:
    assert split_sentences("La BMV cerró al alza. El índice ganó 0.8%.") == [
        "La BMV cerró al alza.",
        "El índice ganó 0.8%.",
    ]


def test_keeps_question_and_exclamation_marks() -> None:
    assert split_sentences("¿Cuánto subió? ¡Un 0.8%!") == ["¿Cuánto subió?", "¡Un 0.8%!"]


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_blank_input_yields_no_sentences(text: str) -> None:
    assert split_sentences(text) == []


def test_sentences_are_trimmed() -> None:
    assert split_sentences("  Una.   Dos.  ") == ["Una.", "Dos."]
