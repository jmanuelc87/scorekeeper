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
    # Both catalog metrics use the Unit scale (identity normalization) with weight 1.0.
    # faithfulness_ragas:    score 0.5 -> norm 0.5, weight 1.0
    # faithfulness_deepeval: score 1.0 -> norm 1.0, weight 1.0
    scores = [_Score("faithfulness_ragas", 0.5), _Score("faithfulness_deepeval", 1.0)]
    expected = (0.5 * 1.0 + 1.0 * 1.0) / (1.0 + 1.0)
    assert turn_score(scores) == pytest.approx(expected)


def test_turn_score_empty_is_none() -> None:
    assert turn_score([]) is None


def test_average_ignores_none() -> None:
    # None is dropped; 1.0 is an excluded outlier, leaving only [0.5].
    assert average([0.5, 1.0, None]) == 0.5
    assert average([None]) is None
    assert average([]) is None
