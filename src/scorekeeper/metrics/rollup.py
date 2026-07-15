"""Score roll-up.

Turn score is the *weighted mean of normalized* metric scores — refining the
plain mean documented in ``docs/data-model.md``. Metrics don't share a scale, so
each raw ``MetricScore.score`` is normalized to [0, 1] via its metric's ``scale``
(looked up in the registry by ``metric_name``) and weighted by the metric's
``weight``. Scenario and platform averages are plain means of their already-
normalized children.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from scorekeeper.metrics.registry import MetricRegistry


class _Scored(Protocol):
    """Anything carrying a ``metric_name`` and a raw ``score`` (e.g. MetricScore)."""

    metric_name: str
    score: float


def turn_score(scores: Iterable[_Scored]) -> float | None:
    """Weighted mean of normalized metric scores for one turn.

    Returns ``None`` when there are no scores. Raises ``KeyError`` if a
    ``metric_name`` is not in the registry.
    """
    numerator = 0.0
    denominator = 0.0
    for score in scores:
        metric = MetricRegistry.get(score.metric_name)
        numerator += metric.scale.normalize(score.score) * metric.weight
        denominator += metric.weight
    return numerator / denominator if denominator else None


def average(values: Iterable[float | None]) -> float | None:
    """Plain mean of the non-null values; ``None`` if there are none."""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


# Scenario and platform averages are the same plain mean of their children.
scenario_average = average
platform_average = average
