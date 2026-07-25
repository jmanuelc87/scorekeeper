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

import numpy as np

from scorekeeper.core.metrics.registry import MetricRegistry


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
    """Mean of the non-null values, excluding exact-0.0 and exact-1.0 outliers.

    ``None`` values are dropped, then any value equal to ``0.0`` or ``1.0`` is
    treated as an outlier and removed before computing the mean with numpy.
    Returns ``None`` if nothing remains.
    """
    present = np.array(
        [v for v in values if v is not None and v != 0.0 and v != 1.0],
        dtype=float,
    )
    return float(np.mean(present)) if present.size else None


# Scenario and platform averages are the same plain mean of their children.
scenario_average = average
platform_average = average
