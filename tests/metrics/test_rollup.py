"""Weighted-normalized rollup."""

from __future__ import annotations

import pytest

from scorekeeper.metrics.rollup import average, turn_score


class _Score:
    """Minimal stand-in for a MetricScore row."""

    def __init__(self, metric_name: str, score: float) -> None:
        self.metric_name = metric_name
        self.score = score


def test_turn_score_is_weighted_mean_of_normalized(registered_metrics) -> None:
    # correccion: Likert score 4 -> norm 0.75, weight 2.0
    # utilidad:   Likert score 5 -> norm 1.0,  weight 1.0
    scores = [_Score("correccion", 4), _Score("utilidad", 5)]
    expected = (0.75 * 2.0 + 1.0 * 1.0) / (2.0 + 1.0)
    assert turn_score(scores) == pytest.approx(expected)


def test_turn_score_empty_is_none() -> None:
    assert turn_score([]) is None


def test_average_ignores_none() -> None:
    assert average([0.5, 1.0, None]) == 0.75
    assert average([None]) is None
    assert average([]) is None
